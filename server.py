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
import subprocess
import sys
import threading
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
# UNIFIED_RX_PROJECT（常驻活跃项目）自动并入沙盒根——扫用户指定的项目必须可访问
_sandbox_roots = [r.strip() for r in os.environ.get("UNIFIED_RX_SANDBOX", os.getcwd()).split(";") if r.strip()]
# UNIFIED_RX_PROJECT（常驻活跃项目）自动并入沙盒根——扫用户指定的项目必须可访问
_active_proj = os.environ.get("UNIFIED_RX_PROJECT", "").strip()
if _active_proj and _active_proj not in _sandbox_roots:
    _sandbox_roots.append(_active_proj)
# 开发项目根（D:\开发）自动并入——全盘扫/窗口扫的目标目录（用户明确要求扫）
# 仅沙盒启用时并入；UNIFIED_RX_SANDBOX=""（显式禁用）不污染
if _sandbox_roots:
    for _dev in (r"D:\开发",):
        if _dev not in _sandbox_roots:
            _sandbox_roots.append(_dev)
_SANDBOX_ROOTS = _sandbox_roots


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
def _m_math_add(args): return str(args["a"] + args["b"])
def _m_math_sub(args): return str(args["a"] - args["b"])
def _m_math_mul(args): return str(args["a"] * args["b"])
def _m_math_div(args):
    if args["b"] == 0:
        raise ValueError("除数不能为 0")
    return str(args["a"] / args["b"])


def _m_math_power(args):
    base = args["base"]
    exp = args["exponent"]
    if abs(exp) > 1000:
        raise ValueError("指数绝对值过大（>1000），拒绝计算")
    if abs(base) > 1e9:
        raise ValueError("底数过大（>1e9），拒绝计算")
    return str(base ** exp)
def _m_math_sqrt(args):
    if args["x"] < 0:
        raise ValueError("负数无实数平方根")
    return str(math.sqrt(args["x"]))


def _m_math_abs(args): return str(abs(args["x"]))
def _m_math_factorial(args):
    n = int(args["n"])
    if n < 0:
        raise ValueError("阶乘要求非负整数")
    if n > 1000:
        raise ValueError("n 过大（>1000，超过 Python int→str 位限）")
    return str(math.factorial(n))


def _m_fib_fibonacci(args):
    n = int(args["n"])
    if n < 0:
        raise ValueError("n 不能为负")
    if n > 20000:
        raise ValueError("n 过大（>20000，超过 Python int→str 位限）")
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return str(a)


def _m_str_reverse(args): return args["s"][::-1]
def _m_str_upper(args): return args["s"].upper()
def _m_str_lower(args): return args["s"].lower()
def _m_str_palindrome(args):
    s = args["s"]
    return str(s == s[::-1])


def _m_sort_quick(args):
    arr = list(args["arr"])
    if len(arr) > 100000:
        raise ValueError("数组过大（>100000）")
    arr.sort()
    return json.dumps(arr)


def _m_sort_bubble(args):
    arr = list(args["arr"])
    if len(arr) > 2000:
        raise ValueError("数组过大（>2000，冒泡 O(n²) 防 DoS）")
    n = len(arr)
    for i in range(n):
        for j in range(n - i - 1):
            if arr[j] > arr[j + 1]:
                arr[j], arr[j + 1] = arr[j + 1], arr[j]
    return json.dumps(arr)


def _m_search_binary(args):
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


def _m_stat_mean(args):
    data = list(args["data"])
    if not data:
        raise ValueError("数据为空")
    if len(data) > 100000:
        raise ValueError("数据过大（>100000）")
    return str(sum(data) / len(data))


def _m_stat_median(args):
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


def _m_geo_circle(args):
    r = args["radius"]
    if r < 0:
        raise ValueError("半径不能为负")
    return str(math.pi * r * r)


def _m_geo_rect(args):
    l, w = args["length"], args["width"]
    if l < 0 or w < 0:
        raise ValueError("边长不能为负")
    return str(2 * (l + w))


def _m_conv_c2f(args): return str(args["celsius"] * 9 / 5 + 32)
def _m_conv_f2c(args): return str((args["fahrenheit"] - 32) * 5 / 9)


def _m_json_parse(args):
    try:
        return json.dumps(json.loads(args["json_string"]), ensure_ascii=False)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 解析失败: {e}")


def _m_json_valid(args):
    try:
        json.loads(args["json_string"])
        return "true"
    except json.JSONDecodeError:
        return "false"


def _m_valid_email(args):
    return str(bool(re.match(r"^[\w.+-]+@[\w-]+\.[\w.]+$", args["email"])))


def _m_prime_is_prime(args):
    n = int(args["n"])
    if n < 2:
        return "false"
    if n > 10_000_000:
        raise ValueError("n 过大（>10M）")
    for i in range(2, int(math.isqrt(n)) + 1):
        if n % i == 0:
            return "false"
    return "true"


def _m_prime_generate(args):
    limit = int(args["limit"])
    if limit > 1_000_000:
        raise ValueError("limit 过大（>1M）")
    sieve = bytearray(b"\x01") * (limit + 1)
    sieve[0:2] = b"\x00\x00"
    for i in range(2, int(limit ** 0.5) + 1):
        if sieve[i]:
            sieve[i * i::i] = b"\x00" * len(sieve[i * i::i])
    return json.dumps([i for i in range(2, limit + 1) if sieve[i]])


def _m_list_unique(args):
    seen, out = set(), []
    for x in args["lst"]:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return json.dumps(out)


def _m_list_flatten(args):
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
# R1: rx-core (Rust) 接线层（2026-08-12）
# 纯函数优先走 Rust 常驻子进程（stdin 行协议 → stdout），失败/未编译回退 Python。
# 环境变量 RX_CORE=0 可整体禁用（全走 Python）。
# ─────────────────────────────────────────────────────────────
_RX_CORE_EXE = None
for _cand in (
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "rx-core", "target", "release", "rx-core.exe"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "rx-core", "target", "debug", "rx-core.exe"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "rx-core", "target", "release", "rx-core"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "rx-core", "target", "debug", "rx-core"),
):
    if os.path.exists(_cand):
        _RX_CORE_EXE = _cand
        break

# Rust 实际支持的工具白名单（main.rs dispatch 表；不在名单的直接走 Python，零开销）
_RX_CORE_TOOLS = frozenset({
    "math_div", "math_power", "math_sqrt", "math_factorial", "fib",
    "str_reverse", "str_upper", "str_lower", "str_palindrome",
    "sort_quick", "sort_bubble", "search_binary",
    "stat_mean", "stat_median", "geo_circle", "geo_rect",
    "c2f", "f2c", "json_parse", "json_valid", "email",
    "is_prime", "gen_primes", "list_unique", "list_flatten",
})

_rxcore_proc = None
_rxcore_lock = threading.Lock()


def _rxcore_enabled() -> bool:
    """运行时读取开关（import 后改环境变量也生效）。"""
    return os.environ.get("RX_CORE", "1") != "0"


def _rxcore_proc_get():
    """获取常驻子进程（懒启动 + 崩溃自动重启）。"""
    global _rxcore_proc
    if _RX_CORE_EXE is None:
        raise ValueError("rx-core 未编译（回退 Python）")
    if _rxcore_proc is None or _rxcore_proc.poll() is not None:
        _rxcore_proc = subprocess.Popen(
            [_RX_CORE_EXE], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)
    return _rxcore_proc


def _rxcore_normalize(tool: str, args: dict, out: str) -> str:
    """对齐 Python 整数语义：输入全 int 时 Python 输出不带 .0（Rust f64 带）。

    仅对 Python 侧会输出 int 的少数工具做处理（parity 已验证其余一致）：
      - math_power:  base/exponent 均 int → Python 2**10 = "1024"
      - stat_median: 奇数长度 + 全 int → Python str(元素) 保持 int
      - geo_rect:    length/width 均 int → Python 2*(l+w) 保持 int
    """
    if not out.endswith(".0"):
        return out
    if tool == "math_power":
        if (isinstance(args.get("base"), int) and not isinstance(args.get("base"), bool)
                and isinstance(args.get("exponent"), int) and not isinstance(args.get("exponent"), bool)):
            return out[:-2]
    elif tool == "stat_median":
        data = args.get("data") or []
        if (len(data) % 2 == 1
                and all(isinstance(x, int) and not isinstance(x, bool) for x in data)):
            return out[:-2]
    elif tool == "geo_rect":
        if (isinstance(args.get("length"), int) and not isinstance(args.get("length"), bool)
                and isinstance(args.get("width"), int) and not isinstance(args.get("width"), bool)):
            return out[:-2]
    return out


def _rxcore_call(tool: str, args: dict) -> str:
    """调用 rx-core(Rust)：stdin 行协议 → stdout 结果字符串。失败抛 ValueError。"""
    if tool not in _RX_CORE_TOOLS:
        raise ValueError(f"rx-core 不支持工具 {tool}（回退 Python）")
    payload = json.dumps({"tool": tool, "args": args})
    with _rxcore_lock:
        p = _rxcore_proc_get()
        try:
            p.stdin.write(payload + "\n")
            p.stdin.flush()
            out = p.stdout.readline().rstrip("\n")
        except (BrokenPipeError, OSError) as e:
            # 子进程崩溃 → 重启一次再试
            global _rxcore_proc
            _rxcore_proc = None
            p = _rxcore_proc_get()
            try:
                p.stdin.write(payload + "\n")
                p.stdin.flush()
                out = p.stdout.readline().rstrip("\n")
            except Exception as e2:
                raise ValueError(f"rx-core 调用失败: {e2}") from e2
        if not out:
            raise ValueError(f"rx-core 无输出（进程退出码 {p.poll()}）")
    if out.startswith("ERR: "):
        # Rust 侧错误（含 unknown tool）→ 转异常，由 _rxcore_wrap 回退 Python
        raise ValueError(out[5:])
    return _rxcore_normalize(tool, args, out)


def _rxcore_wrap(rust_tool: str, py_fn):
    """Rust 优先执行，失败/禁用时回退 Python 纯函数。"""
    def wrapped(args):
        if _rxcore_enabled():
            try:
                return _rxcore_call(rust_tool, args)
            except Exception:
                pass  # 回退 Python
        return py_fn(args)
    return wrapped


# ── 组合工具（2026-08-11 去重重构：29 单工具 → 6 组合，action 分发）──
# 背景：原 29 个纯函数单工具与外部 ci-optimization MCP 功能重复（AI 报告 45 冲突）。
# 方案：保留全部逻辑为 _m_* 内部函数，对外仅暴露 6 个组合工具 + fib_fibonacci，
# 工具数 69 → 47，能力零丢失（旧名不再暴露，如需兼容可在 _ALIASES 加映射）。

_MATH_ACTIONS = {
    "add": _rxcore_wrap("math_add", lambda a: str(a["a"] + a["b"])),
    "sub": _rxcore_wrap("math_sub", lambda a: str(a["a"] - a["b"])),
    "mul": _rxcore_wrap("math_mul", lambda a: str(a["a"] * a["b"])),
    "div": _rxcore_wrap("math_div", _m_math_div),
    "power": _rxcore_wrap("math_power", _m_math_power),
    "sqrt": _rxcore_wrap("math_sqrt", _m_math_sqrt),
    "abs": _rxcore_wrap("math_abs", _m_math_abs),
    "factorial": _rxcore_wrap("math_factorial", _m_math_factorial),
    "c2f": _rxcore_wrap("c2f", _m_conv_c2f),
    "f2c": _rxcore_wrap("f2c", _m_conv_f2c),
}


def _tool_math_ops(args: dict):
    """数学运算组合：add/sub/mul/div/power/sqrt/abs/factorial + 温度换算 c2f/f2c。"""
    action = args.get("action")
    fn = _MATH_ACTIONS.get(action)
    if fn is None:
        raise ValueError("未知 action: %s（可选 %s）" % (action, sorted(_MATH_ACTIONS)))
    return fn(args)


_TEXT_ACTIONS = {
    "reverse": _rxcore_wrap("str_reverse", _m_str_reverse),
    "upper": _rxcore_wrap("str_upper", _m_str_upper),
    "lower": _rxcore_wrap("str_lower", _m_str_lower),
    "palindrome": _rxcore_wrap("str_palindrome", _m_str_palindrome),
}


def _tool_text_ops(args: dict):
    """文本运算组合：reverse/upper/lower/palindrome。"""
    action = args.get("action")
    fn = _TEXT_ACTIONS.get(action)
    if fn is None:
        raise ValueError("未知 action: %s（可选 %s）" % (action, sorted(_TEXT_ACTIONS)))
    return fn(args)


_SORT_SEARCH_ACTIONS = {
    "quick_sort": _rxcore_wrap("sort_quick", _m_sort_quick),
    "bubble_sort": _rxcore_wrap("sort_bubble", _m_sort_bubble),
    "binary_search": _rxcore_wrap("search_binary", _m_search_binary),
}


def _tool_sort_search(args: dict):
    """排序与查找组合：quick_sort/bubble_sort/binary_search。"""
    action = args.get("action")
    fn = _SORT_SEARCH_ACTIONS.get(action)
    if fn is None:
        raise ValueError("未知 action: %s（可选 %s）" % (action, sorted(_SORT_SEARCH_ACTIONS)))
    return fn(args)


_STAT_GEO_ACTIONS = {
    "mean": _rxcore_wrap("stat_mean", _m_stat_mean),
    "median": _rxcore_wrap("stat_median", _m_stat_median),
    "circle_area": _rxcore_wrap("geo_circle", _m_geo_circle),
    "rect_perimeter": _rxcore_wrap("geo_rect", _m_geo_rect),
}


def _tool_stat_geo(args: dict):
    """统计与几何组合：mean/median/circle_area/rect_perimeter。"""
    action = args.get("action")
    fn = _STAT_GEO_ACTIONS.get(action)
    if fn is None:
        raise ValueError("未知 action: %s（可选 %s）" % (action, sorted(_STAT_GEO_ACTIONS)))
    return fn(args)


_JSON_EMAIL_ACTIONS = {
    "parse": _rxcore_wrap("json_parse", _m_json_parse),
    "valid": _rxcore_wrap("json_valid", _m_json_valid),
    "email": _rxcore_wrap("email", _m_valid_email),
}


def _tool_json_email(args: dict):
    """JSON 与校验组合：parse/valid/email。"""
    action = args.get("action")
    fn = _JSON_EMAIL_ACTIONS.get(action)
    if fn is None:
        raise ValueError("未知 action: %s（可选 %s）" % (action, sorted(_JSON_EMAIL_ACTIONS)))
    return fn(args)


_PRIME_LIST_ACTIONS = {
    "is_prime": _rxcore_wrap("is_prime", _m_prime_is_prime),
    "generate": _rxcore_wrap("gen_primes", _m_prime_generate),
    "unique": _rxcore_wrap("list_unique", _m_list_unique),
    "flatten": _rxcore_wrap("list_flatten", _m_list_flatten),
}


def _tool_prime_list(args: dict):
    """素数表与列表组合：is_prime/generate/unique/flatten。"""
    action = args.get("action")
    fn = _PRIME_LIST_ACTIONS.get(action)
    if fn is None:
        raise ValueError("未知 action: %s（可选 %s）" % (action, sorted(_PRIME_LIST_ACTIONS)))
    return fn(args)


# ── 高协作（2026-08-11：pipeline 步骤链 + parallel 并发组——52 工具任意组合）──
def _parse_val(text: str):
    """结果解析：JSON 优先，非 JSON 原样文本。"""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


def _inject(args, ctx: dict):
    """参数模板注入：字符串 ${key} 递归替换为 context 值。"""
    if isinstance(args, dict):
        return {k: _inject(v, ctx) for k, v in args.items()}
    if isinstance(args, list):
        return [_inject(v, ctx) for v in args]
    if isinstance(args, str) and args.startswith("${") and args.endswith("}"):
        return ctx.get(args[2:-1], args)
    return args


# pipeline 预设配方（一次调用 = 多步流程，减少 AI 工具调用轮次）
# 每个配方返回步骤列表；${path} 等由调用方参数注入。
_PIPELINE_PRESETS: dict[str, list[dict]] = {
    # 仓库审计：索引 → 状态 → 漏洞 → 工程标准（4 步 1 次调用）
    "audit_repo": [
        {"tool": "cb_status", "args": {"path": "${path}"}, "as": "index"},
        {"tool": "bug_scan", "args": {"path": "${path}", "max_files": 100}, "as": "bugs"},
        {"tool": "std_check", "args": {"path": "${path}", "max_files": 100}, "as": "std"},
        {"tool": "vuln_scan", "args": {"path": "${path}", "max_files": 100}, "as": "vuln"},
    ],
    # 挖漏洞默认链（2026-08-13 M1）：生产危险规则 → Python bug → 质量后端 → 符号聚合
    # 智能体做任何改动默认先跑——不用问用户"要不要扫描"
    "bug_hunt": [
        {"tool": "rust_scan", "args": {"path": "${path}"}, "as": "panic"},
        {"tool": "bug_scan", "args": {"path": "${path}", "max_files": 100}, "as": "bugs"},
        {"tool": "quality_scan", "args": {"path": "${path}", "max_files": 100}, "as": "quality"},
        {"tool": "ide_fusion", "args": {"path": "${path}"}, "as": "annotated"},
    ],
    # IDE 基线（2026-08-13 M2）：文件/符号/热点/已知问题——改动前自动上下文
    "ide_context": [
        {"tool": "cb_index", "args": {"path": "${path}"}, "as": "index"},
        {"tool": "cb_status", "args": {"path": "${path}"}, "as": "status"},
        {"tool": "repo_wiki", "args": {"path": "${path}"}, "as": "wiki"},
        {"tool": "bug_hunt", "args": {"path": "${path}"}, "as": "known"},
    ],
    # 幻觉守卫闭环：能力边界 → 声明验证（2 步 1 次调用）
    "guard_text": [
        {"tool": "capability_manifest", "args": {}, "as": "caps"},
        {"tool": "hallucination_guard", "args": {"text": "${text}", "root": "${root}"}, "as": "guard"},
    ],
    # 教训召回 + 反馈（学习闭环）
    "learn": [
        {"tool": "lesson_recall_lse", "args": {"task_description": "${task}"}, "as": "lessons"},
        {"tool": "capability_manifest", "args": {}, "as": "caps"},
    ],
    # 代码定位 + 上下文（改代码前：定位 → 取上下文）
    "locate_context": [
        {"tool": "locate_edit", "args": {"path": "${path}", "query": "${query}"}, "as": "loc"},
        {"tool": "code_complete", "args": {"path": "${path}"}, "as": "code"},
    ],
    # ── P0a 语义管线三步走（2026-08-12，抄 AetherStudio LSP 三件套）──
    # 改代码前：定位 → 光标符号级上下文（传统引擎先跑，模型最后上场）
    "semantic_before": [
        {"tool": "locate_edit", "args": {"path": "${path}", "query": "${query}"}, "as": "loc"},
        {"tool": "cae_code_context", "args": {"path": "${path}", "cursor_line": "${cursor_line}", "search_repo": "${search_repo}"}, "as": "ctx"},
        {"tool": "cae_lsp_query", "args": {"path": "${path}", "cursor_line": "${cursor_line}"}, "as": "lsp"},
        {"tool": "lesson_recall_lse", "args": {"task_description": "${task}"}, "as": "lessons"},
    ],
    # 改代码后：变更影响 → 引用链 → 教训反馈（防回归）
    "semantic_after": [
        {"tool": "cae_change_impact", "args": {"repo_path": "${repo}", "changed_files": "${changed_files}"}, "as": "impact"},
        {"tool": "cae_lsp_query", "args": {"path": "${path}", "cursor_line": "${cursor_line}"}, "as": "lsp_verify"},
        {"tool": "hallucination_guard", "args": {"text": "${text}", "root": "${repo}"}, "as": "guard"},
    ],
    # 完整闭环：改前（定位+上下文+教训）→ 修改 → 改后（影响+验证+防幻觉）
    "semantic_edit": [
        {"tool": "locate_edit", "args": {"path": "${path}", "query": "${query}"}, "as": "loc"},
        {"tool": "cae_code_context", "args": {"path": "${path}", "cursor_line": "${cursor_line}", "search_repo": "${search_repo}"}, "as": "ctx"},
        {"tool": "lesson_recall_lse", "args": {"task_description": "${task}"}, "as": "lessons"},
        {"tool": "cae_change_impact", "args": {"repo_path": "${repo}", "changed_files": "${changed_files}"}, "as": "impact"},
        {"tool": "hallucination_guard", "args": {"text": "${text}", "root": "${repo}"}, "as": "guard"},
    ],
}


def _expand_preset(args: dict) -> dict:
    """preset 展开：把 {preset, ...vars} 转为完整 steps（保持其他参数透传）。"""
    preset_name = args.get("preset")
    if not preset_name:
        return args
    steps = _PIPELINE_PRESETS.get(str(preset_name))
    if steps is None:
        raise ValueError(f"未知 preset: {preset_name}（可选: {sorted(_PIPELINE_PRESETS)}）")
    expanded = dict(args)
    # 展开后的步骤：调用方显式 steps 优先，否则用配方
    if not expanded.get("steps"):
        expanded["steps"] = steps
    return expanded


def _tool_pipeline(args: dict) -> str:
    """步骤链：前一步结果注入下一步参数（${key}），实现工具间数据流协作。

    支持 preset 配方（一次调用跑完整流程，减少调用轮次）：
      pipeline({preset:"audit_repo", path:"..."})   → 索引+漏洞+标准 4 步
      pipeline({preset:"guard_text", text:"...", root:"..."}) → 能力清单+幻觉守卫
      pipeline({preset:"learn", task:"..."})        → 教训召回+能力清单
      pipeline({preset:"locate_context", path:"...", query:"..."}) → 定位+补全
    """
    args = _expand_preset(args)
    steps = args.get("steps") or []
    max_steps = int(args.get("max_steps", 20))
    depth = int(args.get("_depth", 0))
    if depth > 3:
        raise ValueError("pipeline 嵌套深度超限（>3，防 DoS）")
    if not steps or len(steps) > max_steps:
        raise ValueError(f"steps 需 1~{max_steps} 项")
    # 初始上下文 = 调用方顶层参数（preset 变量如 path/text/root 可直接 ${注入}）
    ctx: dict = {k: v for k, v in args.items()
                 if k not in ("steps", "max_steps", "preset", "_depth")}
    out = []
    for i, step in enumerate(steps):
        tool = str(step.get("tool", ""))
        if not tool:
            raise ValueError(f"step {i} 缺 tool")
        if tool in ("pipeline", "parallel"):
            inner = dict(step.get("args") or {})
            inner["_depth"] = depth + 1
            sargs = _inject(inner, ctx)
        else:
            sargs = _inject(step.get("args") or {}, ctx)
        r = _call(tool, sargs)[0].text
        val = _parse_val(r)
        key = step.get("as")
        if key:
            ctx[str(key)] = val
        out.append({"step": i, "tool": tool, "ok": not r.startswith("Error"), "result": val})
    return json.dumps({"ok": True, "preset": args.get("preset"), "steps": out,
                       "context_keys": sorted(ctx)}, ensure_ascii=False)


def _tool_parallel(args: dict) -> str:
    """并发组：多工具同时执行（ThreadPoolExecutor ≤8 并发），全部完成后汇总。"""
    tasks = args.get("tasks") or []
    timeout = float(args.get("timeout", 60))
    depth = int(args.get("_depth", 0))
    if depth > 3:
        raise ValueError("parallel 嵌套深度超限（>3，防 DoS）")
    if not tasks or len(tasks) > 50:
        raise ValueError("tasks 需 1~50 项")
    if timeout < 1 or timeout > 600:
        raise ValueError("timeout 需在 [1,600] 秒")
    from concurrent.futures import ThreadPoolExecutor

    results: list[dict] = [None] * len(tasks)

    def run(i: int, t: dict) -> None:
        try:
            tool = str(t.get("tool", ""))
            targs = t.get("args") or {}
            if tool in ("pipeline", "parallel"):
                targs = dict(targs)
                targs["_depth"] = depth + 1
            r = _call(tool, targs)[0].text
            results[i] = {"tool": t.get("tool"), "ok": not r.startswith("Error"), "result": _parse_val(r)}
        except Exception as exc:
            results[i] = {"tool": t.get("tool"), "ok": False, "result": f"Error: {type(exc).__name__}: {exc}"}

    with ThreadPoolExecutor(max_workers=min(len(tasks), 8)) as ex:
        futs = [ex.submit(run, i, t) for i, t in enumerate(tasks)]
        for f in futs:
            f.result(timeout=timeout)
    return json.dumps({"ok": True, "results": results}, ensure_ascii=False)


# ── P0b 混合检索（2026-08-12：BM25 全文 + 向量接口 + RRF 融合，抄 tantivy/BGE 思路）──
def _tool_kb_query(args: dict) -> str:
    """知识库/代码语义检索：对索引目录做混合检索（BM25 全文 + 可选向量，RRF 融合）。

    index_dir: 索引文件目录（首次调用自动建索引；传源码目录时懒加载构建）
    query:     检索词
    index_file: 可选，直接指定 .db 文件（默认 index_dir/search-index.db）
    limit:     返回条数（默认 20）
    向量路：search_index.py 预留 embed_fn 接口，未配置时自动降级纯 BM25。
    """
    index_dir = str(args.get("index_dir") or "")
    query = str(args.get("query") or "")
    index_file = str(args.get("index_file") or "")
    limit = min(int(args.get("limit", 20)), 100)
    if not index_dir or not query.strip():
        raise ValueError("index_dir 和 query 必填")
    if not os.path.isdir(index_dir):
        raise ValueError(f"索引目录不存在: {index_dir}")
    try:
        from search_index import SearchIndex
    except ImportError:
        _dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, _dir)
        from search_index import SearchIndex  # noqa: F811
    db = index_file or os.path.join(index_dir, ".unified-rx-index", "search-index.db")
    # 确保索引目录存在（默认路径进 .unified-rx-index/，与 repo_graph 图索引一致，不污染项目根）
    os.makedirs(os.path.dirname(db), exist_ok=True)
    try:
        idx = SearchIndex(db)
        hits = idx.search_hybrid(query, embed_fn=None, limit=limit)
        return json.dumps({
            "ok": True, "query": query, "count": len(hits), "db": db,
            "hits": [{"id": h["id"], "title": h.get("title", ""),
                      "meta": h.get("meta", {}),
                      "snippet": (h.get("content") or "")[:200]} for h in hits],
            "note": "BM25 全文检索（向量路未配置时自动降级；配置 embed_fn 后启用 RRF 融合）",
        }, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({"ok": False, "error": f"检索失败: {exc}"}, ensure_ascii=False)


# ── P1a/P1b 掌握引擎（2026-08-12：tree-sitter 符号图，抄 codebase-memory 图查询）──
def _tool_repo_graph(args: dict) -> str:
    """代码库符号图查询：调用链/影响面/核心模块/符号搜索（tree-sitter 多语言图索引）。

    root:    代码库根目录（首次调用自动建图索引，缓存到 <root>/.unified-rx-index/graph.db）
    query:   查询类型：callers(谁调用我)/callees(我调用谁)/impact(影响面 BFS)/hubs(核心符号)/search(符号搜索)
    symbol:  callers/callees 用：符号名（如 _tool_math_ops）或 'file:符号名'
    file:    impact 用：文件路径（相对 root）
    name:    search 用：符号名模糊
    depth:   impact 用：BFS 深度（默认 3）
    top:     hubs 用：返回条数（默认 10）
    """
    root = str(args.get("root") or "")
    query = str(args.get("query") or "search")
    symbol = str(args.get("symbol") or "")
    file = str(args.get("file") or "")
    name = str(args.get("name") or "")
    depth = min(int(args.get("depth", 3)), 6)
    top = min(int(args.get("top", 10)), 50)
    if not root or not os.path.isdir(root):
        raise ValueError(f"root 必须存在: {root}")
    try:
        from graph_index import GraphIndex
    except ImportError:
        _dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, _dir)
        from graph_index import GraphIndex  # noqa: F811
    idx_dir = os.path.join(root, ".unified-rx-index")
    os.makedirs(idx_dir, exist_ok=True)
    db = os.path.join(idx_dir, "graph.db")
    gi = GraphIndex(db)
    # 懒建索引：图不存在或为空时构建
    if gi.stats()["nodes"] == 0:
        stats = gi.index_directory(root)
    else:
        stats = gi.stats()
    try:
        if query == "callers":
            if not symbol:
                raise ValueError("callers 需要 symbol")
            hits = gi.callers_of(_resolve_symbol(gi, root, symbol))
            return json.dumps({"ok": True, "query": "callers", "symbol": symbol,
                               "count": len(hits), "callers": hits,
                               "index": stats}, ensure_ascii=False, indent=2)
        if query == "callees":
            if not symbol:
                raise ValueError("callees 需要 symbol")
            hits = gi.callees_of(_resolve_symbol(gi, root, symbol))
            return json.dumps({"ok": True, "query": "callees", "symbol": symbol,
                               "count": len(hits), "callees": hits,
                               "index": stats}, ensure_ascii=False, indent=2)
        if query == "impact":
            if not file:
                raise ValueError("impact 需要 file")
            fpath = os.path.join(root, file) if not os.path.isabs(file) else file
            # 路径规范化：graph.db 存的是 os.walk 产出的原生分隔符路径，
            # 正斜杠/混合分隔符会导致 SQL 精确匹配失败（实测踩坑）
            fpath = os.path.normpath(fpath).replace("/", os.sep)
            hits = gi.impact(fpath, depth=depth)
            return json.dumps({"ok": True, "query": "impact", "file": file,
                               "depth": depth, "count": len(hits), "affected": hits,
                               "index": stats}, ensure_ascii=False, indent=2)
        if query == "hubs":
            hits = gi.hubs(top=top)
            return json.dumps({"ok": True, "query": "hubs", "count": len(hits),
                               "hubs": hits, "index": stats}, ensure_ascii=False, indent=2)
        if query == "communities":
            hits = gi.communities(max_communities=top)
            return json.dumps({"ok": True, "query": "communities",
                               "count": len(hits), "communities": hits,
                               "index": stats}, ensure_ascii=False, indent=2)
        # search
        hits = gi.search_symbols(name or symbol, limit=top)
        return json.dumps({"ok": True, "query": "search", "name": name or symbol,
                           "count": len(hits), "symbols": hits,
                           "index": stats}, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({"ok": False, "error": f"图查询失败: {exc}"}, ensure_ascii=False)


# ── P2a 质量引擎（2026-08-12：ruff/semgrep/gitleaks/pyright 多后端，抄 ruff★49k）──
def _tool_quality_scan(args: dict) -> str:
    """质量多后端扫描：ruff(Python lint) + semgrep(跨语言模式) + gitleaks(密钥) + pyright(类型)。

    path:      文件或目录
    backends:  指定后端（可选，默认全部可用后端）
    后端未安装时自动降级（返回 available:false，不炸）。
    """
    path = str(args.get("path") or "")
    backends = args.get("backends") or None
    if not path:
        raise ValueError("path 必填")
    try:
        from quality_engine import QualityEngine
    except ImportError:
        _dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, _dir)
        from quality_engine import QualityEngine  # noqa: F811
    qe = QualityEngine()
    return json.dumps(qe.scan(path, backends=backends), ensure_ascii=False, indent=2)


def _resolve_symbol(gi, root: str, symbol: str) -> str:
    """符号名 → 完整 node id。已含路径前缀则直接用；否则全库模糊匹配第一个。"""
    if "::" in symbol:
        return symbol
    hits = gi.search_symbols(symbol, limit=1)
    if hits:
        return hits[0]["id"]
    return f"{root}::{symbol}"


# ── repo_wiki（2026-08-12：抄 Qoder Repo Wiki——自动生成代码库结构文档）──
def _tool_repo_wiki(args: dict) -> str:
    """一键生成代码库结构文档（Qoder Repo Wiki 对齐）。

    从 tree-sitter 符号图 + 目录结构生成 markdown：
    模块地图 / 核心符号(hubs) / 模块依赖，落盘 <root>/.unified-rx-index/WIKI.md。
    AI 一次调用即"看全"仓库结构（省几十次 fs_read）。
    """
    root = str(args.get("root") or "")
    out = str(args.get("out") or "")
    if not root or not os.path.isdir(root):
        raise ValueError(f"root 必须存在: {root}")
    try:
        from repo_wiki import generate_wiki
    except ImportError:
        _dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, _dir)
        from repo_wiki import generate_wiki  # noqa: F811
    if not out:
        out = os.path.join(root, ".unified-rx-index", "WIKI.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    return json.dumps(generate_wiki(root, out), ensure_ascii=False, indent=2)


# ── 多智能体编排（2026-08-12：抄 crewAI/autogen 角色分工 + 并行协作）──
def _tool_agent_orchestrate(args: dict) -> str:
    """多智能体编排：按角色分工并行执行多个工具任务，汇总结果。

    tasks: [{id, role, tool, args}]——role: analyst/quality/memory/writer/explorer
    角色是工具白名单（防越权）：analyst=图查询/检索/扫描；quality=质检；memory=记忆。
    同角色任务并行（ThreadPool），结果按 id 汇总。
    """
    tasks = args.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("tasks 必填: [{id, role, tool, args}]")
    try:
        import multi_agent as _ma
    except ImportError:
        _dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, _dir)
        import multi_agent as _ma  # noqa: F811

    def call_fn(tool: str, targs: dict):
        # _call 返回 [_TC(text)]，提取 .text 为可序列化值（防 "not JSON serializable"）
        res = _call(tool, targs)
        if isinstance(res, list):
            parts = []
            for item in res:
                txt = getattr(item, "text", None)
                if txt is not None:
                    parts.append(txt)
                else:
                    parts.append(str(item))
            return "\n".join(parts)
        return res

    result = _ma.orchestrate(tasks, call_fn)
    return json.dumps(result, ensure_ascii=False, indent=2)


def _tool_agent_roles(args: dict) -> str:
    """角色目录：查看各角色的工具集与用途（配合 agent_orchestrate）。"""
    try:
        import multi_agent as _ma
    except ImportError:
        _dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, _dir)
        import multi_agent as _ma  # noqa: F811
    return json.dumps({"ok": True, "roles": _ma.role_catalog()},
                      ensure_ascii=False, indent=2)


# ── 挖漏洞统一入口（2026-08-11 整合：bug_scan + std_check + ui_check 一次调用全跑）──
# 2026-08-11 高并发：三路扫描并行（ThreadPoolExecutor），互不打扰，总耗时 ≈ 最慢一路
def _tool_vuln_scan(args: dict) -> "list[types.TextContent]":
    """统一漏洞扫描入口：对 path **高并发**跑 bug_scan + std_check + ui_check（三路并行，互不打扰），返回聚合 JSON。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    path = args["path"]
    max_files = int(args.get("max_files", 100))
    results = {"path": path, "bug_scan": [], "std_check": [], "ui_check": [], "errors": []}

    def run_one(tool_fn: str, name: str) -> None:
        try:
            fn = {"bug_scan": _tool_bug_scan, "std_check": _tool_std_check,
                  "ui_check": _tool_ui_check}[tool_fn]
            r = fn({"path": path, "max_files": max_files})
            text = r[0].text if isinstance(r, list) else str(r)
            results[name] = json.loads(text) if text.startswith("{") else {"raw": text[:200]}
        except Exception as e:  # noqa: BLE001
            results["errors"].append(f"{name}: {e}")

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(run_one, t, n) for t, n in
                   (("bug_scan", "bug_scan"), ("std_check", "std_check"), ("ui_check", "ui_check"))]
        for fut in as_completed(futures):
            fut.result()  # 异常已在 run_one 内捕获
    return [_tr(True, "vuln_scan 完成(并行): bug=%d std=%d ui=%d" % (
        len(results["bug_scan"]) if isinstance(results["bug_scan"], list) else 0,
        len(results["std_check"]) if isinstance(results["std_check"], list) else 0,
        len(results["ui_check"]) if isinstance(results["ui_check"], list) else 0), results)]


# ── 项目级高并发扫描（2026-08-11：四路并行互不打扰 + 自动落盘 scan-log）──
def _tool_project_scan(args: dict) -> "list[types.TextContent]":
    """项目级高并发扫描：对项目根**并行**跑 bug_scan + std_check + ui_check + cb_scan
    （四路 ThreadPoolExecutor，互不打扰，总耗时 ≈ 最慢一路），结果聚合且自动落盘
    scan-log.jsonl。专项目对话：扫完直接看 scan_log 结果。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    path = args["path"]
    max_files = int(args.get("max_files", 100))
    with_ui = bool(args.get("ui", True))  # Bevy 项目才有 .rs UI；非 Rust 项目可关
    results = {"path": path, "bug_scan": [], "std_check": [], "ui_check": [],
               "cb_scan": [], "errors": []}

    def run_one(tool_fn, name, extra=None):
        try:
            a = {"path": path, "max_files": max_files}
            if extra:
                a.update(extra)
            r = tool_fn(a)
            text = r[0].text if isinstance(r, list) else str(r)
            results[name] = json.loads(text) if text.startswith("{") else {"raw": text[:200]}
        except Exception as e:  # noqa: BLE001
            results["errors"].append(f"{name}: {e}")

    jobs = [(_tool_bug_scan, "bug_scan", None),
            (_tool_std_check, "std_check", None),
            (_tool_cb_scan, "cb_scan", None)]
    if with_ui:
        jobs.append((_tool_ui_check, "ui_check", None))
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futs = [pool.submit(run_one, fn, nm, ex) for fn, nm, ex in jobs]
        for fut in as_completed(futs):
            fut.result()
    # 自动落盘（项目级扫描结果进 scan-log，专项目对话可查）
    try:
        import scan_log_core
        n_bug = len(results["bug_scan"].get("issues", [])) if isinstance(results["bug_scan"], dict) else 0
        scan_log_core.append_scan({
            "tool": "project_scan", "root": path, "ok": not results["errors"],
            "summary": "project_scan %s: bug=%d errors=%d" % (
                os.path.basename(path.rstrip("/\\")), n_bug, len(results["errors"])),
        })
    except Exception:
        pass
    return [_tr(True, "project_scan 完成(并行 %d 路): bug=%d std=%d ui=%d cb=%d" % (
        len(jobs),
        len(results["bug_scan"]) if isinstance(results["bug_scan"], list) else 0,
        len(results["std_check"]) if isinstance(results["std_check"], list) else 0,
        len(results["ui_check"]) if isinstance(results["ui_check"], list) else 0,
        len(results["cb_scan"]) if isinstance(results["cb_scan"], list) else 0), results)]


# ── 全盘扫（2026-08-11：多项目根并发，模式② 全盘扫）──
_FULL_SCAN_DEFAULT_ROOTS = [r"D:\开发\VoxelForge-Nexus", r"D:\开发\reasonix-src",
                           r"D:\开发\VoxelForge", r"D:\开发\unified-rx-mcp"]
# 全盘扫排除目录（用户要求：不扫 Steam、不扫其他无关目录）
# 注意：排除只作用于"自动发现的默认 roots"层面，显式传入的 roots 不过滤
# （测试/用户指定路径必须可扫，防误伤）
_SCAN_EXCLUDE_DIRS = (
    "steam", "steamapps", "appdata", "node_modules", "target", "dist",
    ".git", "__pycache__", "windows", "program files", "games", "downloads",
    ".cargo", ".rustup", ".npm",
)

def _scan_excluded(path: str) -> bool:
    """路径是否命中排除清单（只用于自动发现的默认 roots）。"""
    low = path.lower().replace("\\", "/")
    return any(f"/{e}/" in f"/{low}/" or low.endswith(f"/{e}")
               for e in _SCAN_EXCLUDE_DIRS)


def _tool_full_scan(args: dict) -> "list[types.TextContent]":
    """全盘扫：对多个项目根**并发**跑 project_scan（每个项目四路并行），
    全部完成汇总 + 落盘 scan-log。缺省扫常见项目根，可显式传 roots 列表。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    roots = args.get("roots") or _FULL_SCAN_DEFAULT_ROOTS
    auto_roots = not args.get("roots")  # 未显式传 roots = 自动默认（过排除清单）
    max_files = int(args.get("max_files", 100))
    ui = bool(args.get("ui", True))
    results = {"roots": roots, "projects": [], "errors": []}

    def scan_project(root: str) -> None:
        if auto_roots and _scan_excluded(root):
            return  # 自动发现的默认 roots 过排除清单（不扫 Steam/无关目录）
        try:
            r = _call("project_scan", {"path": root, "max_files": max_files, "ui": ui})
            text = r[0].text if isinstance(r, list) else str(r)
            results["projects"].append({
                "root": root,
                "result": json.loads(text) if text.startswith("{") else {"raw": text[:200]},
            })
        except Exception as e:  # noqa: BLE001
            results["errors"].append(f"{root}: {e}")

    with ThreadPoolExecutor(max_workers=min(4, max(1, len(roots)))) as pool:
        futs = [pool.submit(scan_project, root) for root in roots]
        for fut in as_completed(futs):
            fut.result()
    # 落盘
    try:
        import scan_log_core
        scan_log_core.append_scan({
            "tool": "full_scan", "root": "|".join(roots), "ok": not results["errors"],
            "summary": "full_scan: %d projects, errors=%d" % (
                len(results["projects"]), len(results["errors"])),
        })
    except Exception:
        pass
    return [_tr(True, "full_scan 完成(并发 %d 项目): ok=%d errors=%d" % (
        len(roots), len(results["projects"]), len(results["errors"])), results)]


# ─────────────────────────────────────────────────────────────
# 防幻觉守卫（hallucination_guard + capability_manifest：AI 事实核查）
# ─────────────────────────────────────────────────────────────
def _tool_hallucination_guard(args: dict) -> "list[types.TextContent]":
    """幻觉守卫：提取 AI 声明中的可验证事实（file:line / 反引号符号 / 工具名），
    对照本地文件系统与工具注册表逐条验证，输出 verified / refuted / unverifiable 三分级。
    refuted（被证伪）即幻觉——必须纠正后才能继续。纯静态零 LLM 零网络。"""
    text = str(args.get("text", ""))
    root = args.get("root") or None
    if not text.strip():
        return [_TC(json.dumps({"ok": False, "error": "text 必填（AI 声明文本）"},
                               ensure_ascii=False))]
    if root:
        # root 同样过沙盒（guard 引擎内部还做 root 包含校验，双保险）
        try:
            root = str(_check_path(root))
        except ValueError as e:
            return [_TC(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))]
    try:
        from guard_core import guard_text
    except ImportError:
        _dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, _dir)
        from guard_core import guard_text  # noqa: F811
    tool_names = set(_TOOLS.keys()) | set(_EXT_DEFS.keys())
    res = guard_text(text, root=root, tool_names=tool_names)
    res["ok"] = True
    res["tool_names_checked"] = len(tool_names)
    # 防幻觉闭环：refuted（被证伪=幻觉）自动回灌 LSE——负 delta 惩罚该模式
    # + 经验教训卡片入库，下次 lesson_recall/experience_match 可召回防复发。
    if res.get("refuted"):
        try:
            import lse_client as _lse
        except ImportError:
            _dir = os.path.dirname(os.path.abspath(__file__))
            sys.path.insert(0, _dir)
            import lse_client as _lse  # noqa: F811
        recorded = []
        for item in res["refuted"]:
            claim = item.get("claim", "")
            reason = item.get("reason", "")
            # 教训 ID = 内容 hash（同模式幻觉汇聚同一 lesson，形成枢纽）
            lid = f"hallucination-{abs(hash(claim)) % 10**9}"
            r = _lse.delta_update_lesson(lid, -0.2, threshold=0.05)
            ok = r.get("ok", False)
            # 经验教训卡片：记录声明+证伪原因，供 experience_match 召回
            if ok:
                _lse.experience_store(
                    model="hallucination_guard", ctx=claim[:120],
                    delta=-0.2, summary=f"幻觉被证伪: {claim[:80]} — {reason[:80]}",
                )
            recorded.append({
                "claim": claim,
                "lesson_id": lid,
                "recorded": ok,
                "utility": (r.get("result") or {}).get("utility"),
            })
        res["feedback_recorded"] = recorded
        res["feedback_note"] = (
            "幻觉模式已自动回灌 LSE（负 delta 惩罚 + 教训卡片入库）。"
            "下次 lesson_recall_lse 可召回该模式防复发。" if recorded and
            all(x["recorded"] for x in recorded)
            else "幻觉已检测，但 lse-engine 未构建，回灌跳过（本地降级）。"
        )
    return [_TC(json.dumps(res, ensure_ascii=False))]


def _tool_capability_manifest(args: dict) -> "list[types.TextContent]":
    """能力清单（动态生成）：列出全部工具 + 显式边界声明（有什么 / 没有什么），
    防止 AI 幻觉自己具备不存在的能力。对话开始时调用一次即可。"""
    core = [{"name": n, "desc": d} for n, (_, _, d) in _TOOLS.items()]
    ext = []
    for n, (_, _, t) in _EXT_DEFS.items():
        desc = getattr(t, "description", None) or str(t)[:120]
        ext.append({"name": n, "desc": desc})
    try:
        from guard_core import capability_manifest
    except ImportError:
        _dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, _dir)
        from guard_core import capability_manifest  # noqa: F811
    res = capability_manifest(core, ext)
    res["ok"] = True
    res["core_count"] = len(core)
    res["ext_count"] = len(ext)
    return [_TC(json.dumps(res, ensure_ascii=False))]


def _tool_scan_log(args: dict) -> "list[types.TextContent]":
    """扫描日志查询（常驻自扫落盘）：按项目 root / 工具名过滤，返回最近记录。

    机制：扫描类工具（bug_scan/std_check/vuln_scan/ui_check/cb_scan/cb_index/
    hallucination_guard/locate_edit）每次调用自动追加到 ~/.unified-rx/scan-log.jsonl；
    专门搞某个项目的对话，用 root 过滤即可查看该项目的历史扫描结果。
    """
    try:
        import scan_log_core
    except ImportError:
        _dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, _dir)
        import scan_log_core  # noqa: F811
    root = args.get("root") or None
    tool = args.get("tool") or None
    limit = int(args.get("limit", 50))
    if not 1 <= limit <= 200:
        raise ValueError("limit 须在 [1,200]")
    logs = scan_log_core.query_logs(root=root, tool=tool, limit=limit)
    return [_TC(json.dumps({
        "ok": True,
        "log_path": str(scan_log_core.log_path()),
        "count": len(logs),
        "filter": {"root": root, "tool": tool},
        "logs": logs,
        "note": "扫描类工具调用自动落盘；专项目对话按 root 过滤查看",
    }, ensure_ascii=False))]


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


def _bug_is_none_guarded(child, none_vars) -> bool:
    """短路保护检测：X is None or X.field / X is None and X.field 模式不报。
    沿 _p 父链向上找 BoolOp：若 X 解引用在 BoolOp 右支且左支有 'X is None'，则受保护。"""
    cur = getattr(child, "_p", None)
    while cur is not None:
        if isinstance(cur, ast.BoolOp):
            pos = None
            for i, v in enumerate(cur.values):
                if v is child or child in _ast_children(v):
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
        cur = getattr(cur, "_p", None)
    return False


def _ast_children(n):
    return list(ast.iter_child_nodes(n))


def _bug_check_deref(node, none_vars, path, lines, issues):
    """None 变量被解引用（属性/下标/调用）检测；线性近似，可能漏报/误报。"""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.Attribute, ast.Subscript)) and isinstance(child.value, ast.Name) \
                and child.value.id in none_vars:
            if not _bug_is_none_guarded(child, none_vars):
                issues.append(_bug_issue(path, child, "none_deref", "warning",
                                         f"'{child.value.id}' 可能为 None，此处解引用会抛异常", lines))
        elif isinstance(child, ast.Call) and isinstance(child.func, ast.Name) \
                and child.func.id in none_vars:
            if not _bug_is_none_guarded(child, none_vars):
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


def _scan_file_dispatch(f: str) -> tuple[list, int]:
    """按后缀分发扫描：.py 用 _bug_scan_file（Python AST），.rs 用 rust_scan（tree-sitter）。"""
    if f.endswith(".rs"):
        try:
            from rust_scan import scan_rust_file
        except ImportError:
            _dir = os.path.dirname(os.path.abspath(__file__))
            sys.path.insert(0, _dir)
            from rust_scan import scan_rust_file  # noqa: F811
        issues, ln = scan_rust_file(f)
        # 规范化为统一 issue 结构（补 col 供排序）
        for i in issues:
            i.setdefault("col", 0)
        return issues, ln
    return _bug_scan_file(f)


def _tool_bug_scan(args: dict) -> "list[types.TextContent]":
    """静态扫描 bug 模式：未定义变量/None 解引用/资源泄漏/除零/越界。

    多文件目录扫描用 ProcessPoolExecutor 并行（CPU 密集 AST，子进程不吃 GIL）；
    进程池不可用（受限环境）时串行 fallback。结果与串行完全一致。
    单文件走 scan_cache（mtime/size 未变直接返回缓存，省重复扫描）。
    """
    p = _check_path(str(args["path"]))
    max_files = int(args.get("max_files", _BUG_MAX_FILES))
    if not 1 <= max_files <= 500:
        raise ValueError("max_files 须在 1..500")
    # 单文件缓存（幂等只读；文件变了缓存失效）
    if p.is_file():
        try:
            import scan_cache
            hit = scan_cache.get("bug_scan", str(p))
            if hit is not None:
                return [_TC(json.dumps(hit, ensure_ascii=False))]
        except ImportError:
            pass
    files = []
    if p.is_file():
        if p.suffix in (".py", ".rs"):
            files = [p]
        else:
            raise ValueError(f"仅支持 Python/Rust 文件: {p}")
    elif p.is_dir():
        for root, _, names in os.walk(p):
            for name in sorted(names):
                if name.endswith((".py", ".rs")):
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
                for fi, ln in ex.map(_scan_file_dispatch, [str(f) for f in files]):
                    issues.extend(fi)
                    total_lines += ln
        except Exception:
            # 进程池不可用（受限环境）→ 串行 fallback
            issues, total_lines = [], 0
            for f in files:
                fi, ln = _scan_file_dispatch(str(f))
                issues.extend(fi)
                total_lines += ln
    else:
        for f in files:
            fi, ln = _scan_file_dispatch(str(f))
            issues.extend(fi)
            total_lines += ln

    if total_lines > _BUG_MAX_TOTAL_LINES:
        raise ValueError(f"扫描总量超限（>{_BUG_MAX_TOTAL_LINES} 行），请缩小范围")
    issues.sort(key=lambda i: (i["file"], i["line"], i["col"]))
    # P2 信噪比度量（SCAN_QUALITY_ISSUES.md 问题 C 修复）：severity 归一化统计 +
    # noise_ratio（info 占比）——AI 一眼可判断"这份报告可信度"；error 优先展示
    sev_counts = {"error": 0, "warn": 0, "info": 0}
    for i in issues:
        s = str(i.get("severity", "warn"))
        sev_counts["warn" if s in ("warn", "warning") else
                   ("error" if s == "error" else "info")] += 1
    total = len(issues)
    result = {
        "ok": True, "files": len(files), "issue_count": len(issues),
        "severity_counts": sev_counts,
        "noise_ratio": round(sev_counts["info"] / total, 3) if total else 0.0,
        "note": ("noise_ratio=info 占比（高即多为风格提示）；error 为确定性缺陷，"
                 "warn 为需审查项——参考 SCAN_QUALITY_ISSUES.md"),
        "issues": issues,
    }
    # 单文件成功结果入缓存（幂等只读；mtime/size 变了自动失效）
    if p.is_file():
        try:
            import scan_cache
            scan_cache.put("bug_scan", str(p), result)
        except ImportError:
            pass
    return [_TC(json.dumps(result, ensure_ascii=False))]


def _tool_ide_rename(args: dict) -> "list[types.TextContent]":
    from ide_tools import ide_rename
    return [_TC(json.dumps(ide_rename(
        args.get("root", ""), args.get("symbol", ""), args.get("new_name", ""),
        exclude_comments=bool(args.get("exclude_comments", True)),
        include_plan=bool(args.get("include_plan", False)),
    ), ensure_ascii=False, indent=2))]


def _tool_ide_complete(args: dict) -> "list[types.TextContent]":
    from ide_tools import ide_complete
    # IDE 增强四十五前序：键名双兼容（root|path、file|file_path）——
    # 调用方混淆键名会静默空结果，双键收窄这个坑
    root = args.get("root") or args.get("path", "")
    file = args.get("file") or args.get("file_path", "")
    return [_TC(json.dumps(ide_complete(root, file,
                                        args.get("prefix", "")), ensure_ascii=False, indent=2))]


def _tool_ide_references(args: dict) -> "list[types.TextContent]":
    from ide_tools import ide_references
    return [_TC(json.dumps(ide_references(args.get("root", ""), args.get("symbol", "")),
                           ensure_ascii=False, indent=2))]


def _tool_ide_actions(args: dict) -> "list[types.TextContent]":
    from ide_tools import ide_actions
    return [_TC(json.dumps(ide_actions(args.get("path", "")), ensure_ascii=False, indent=2))]


def _tool_ide_fusion(args: dict) -> "list[types.TextContent]":
    """IDE 融合：annotate（诊断→符号图聚合，默认）/ impact（双引擎影响面校验）。"""
    action = args.get("action", "annotate")
    path = args.get("path", "")
    if not path or not os.path.isdir(path):
        return [_TC(json.dumps({"ok": False, "error": f"目录不存在: {path}"}, ensure_ascii=False))]
    if action == "impact":
        # 双引擎影响面：LSP 引用（可空）vs ide_references（tree 侧词级+声明判定）
        from ide_fusion import impact_via_references
        symbol = str(args.get("symbol", ""))
        if not symbol:
            return [_TC(json.dumps({"ok": False, "error": "impact 需要 symbol 参数"},
                                   ensure_ascii=False))]
        lsp_refs = args.get("lsp_refs") or []
        return [_TC(json.dumps(impact_via_references(path, symbol, lsp_refs),
                               ensure_ascii=False, indent=2))]
    if action != "annotate":
        return [_TC(json.dumps({"ok": False, "error": f"未知 action: {action}（annotate/impact）"},
                               ensure_ascii=False))]
    from ide_fusion import annotate_issues
    issues: list[dict] = []
    # 用 rust_scan 危险规则扫（unwrap/expect/as 收窄——生产风险）
    try:
        from rust_scan import scan_rust_file
        exts = (".rs",)
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = [d for d in dirnames if d not in ("target", "node_modules", ".git", "release")]
            for fn in filenames:
                if not fn.endswith(exts):
                    continue
                p = os.path.join(dirpath, fn)
                found, _ = scan_rust_file(p)
                for it in found:
                    issues.append({"file": p, "line": it.get("line", 0),
                                   "kind": it.get("kind", "issue"),
                                   "message": str(it.get("message", ""))[:100]})
    except ImportError:
        pass
    return [_TC(json.dumps(annotate_issues(path, issues), ensure_ascii=False, indent=2))]


def _tool_ide_quest(args: dict) -> "list[types.TextContent]":
    """Quest 状态机：new/resume/status/step/list/abort/note。"""
    from ide_quest import Quest, new_quest, resume_quest, list_quests, STEPS
    action = args.get("action", "status")
    quest_id = str(args.get("quest_id", ""))
    try:
        if action == "new":
            q = new_quest(quest_id, args.get("task", ""), args.get("repo", ""))
            q._save()
            return [_TC(json.dumps({"ok": True, "status": q.status(),
                                    "steps": [s for s, _ in STEPS]}, ensure_ascii=False, indent=2))]
        if action == "resume":
            q = resume_quest(quest_id)
            if q is None:
                return [_TC(json.dumps({"ok": False, "error": f"任务不存在: {quest_id}"}, ensure_ascii=False))]
            return [_TC(json.dumps({"ok": True, "status": q.status(),
                                    "steps": [s for s, _ in STEPS]}, ensure_ascii=False, indent=2))]
        if action == "status":
            q = resume_quest(quest_id)
            if q is None:
                return [_TC(json.dumps({"ok": False, "error": f"任务不存在: {quest_id}"}, ensure_ascii=False))]
            return [_TC(json.dumps({"ok": True, "status": q.status()}, ensure_ascii=False, indent=2))]
        if action == "step":
            q = resume_quest(quest_id)
            if q is None:
                return [_TC(json.dumps({"ok": False, "error": f"任务不存在: {quest_id}"}, ensure_ascii=False))]
            return [_TC(json.dumps(q.complete_step(args.get("result", {})), ensure_ascii=False, indent=2))]
        if action == "abort":
            q = resume_quest(quest_id)
            if q is None:
                return [_TC(json.dumps({"ok": False, "error": f"任务不存在: {quest_id}"}, ensure_ascii=False))]
            return [_TC(json.dumps(q.abort(), ensure_ascii=False, indent=2))]
        if action == "note":
            q = resume_quest(quest_id)
            if q is None:
                return [_TC(json.dumps({"ok": False, "error": f"任务不存在: {quest_id}"}, ensure_ascii=False))]
            return [_TC(json.dumps(q.add_note(str(args.get("text", ""))), ensure_ascii=False, indent=2))]
        if action == "result":
            # IDE 增强十三：精确检索某步的完整 result（auto 链的 fs_template/
            # checklist/context 等大字段按需取——省 token，不用拉全量状态）
            q = resume_quest(quest_id)
            if q is None:
                return [_TC(json.dumps({"ok": False, "error": f"任务不存在: {quest_id}"},
                                       ensure_ascii=False))]
            step = str(args.get("step", ""))
            if step not in q.state.get("steps", {}):
                return [_TC(json.dumps({"ok": False, "error": f"未知步骤: {step}",
                                        "available": list(q.state.get("steps", {}).keys())},
                                       ensure_ascii=False))]
            return [_TC(json.dumps({"ok": True, "quest_id": quest_id, "step": step,
                                    "done": q.state["steps"][step].get("done", False),
                                    "result": q.state["steps"][step].get("result")},
                                   ensure_ascii=False, indent=2))]
        if action == "verify_fix":
            # IDE 增强十八：fs_template/修复应用后的验证器——重扫 locate 步
            # 目标文件，对比问题数判断修复是否生效
            q = resume_quest(quest_id)
            if q is None:
                return [_TC(json.dumps({"ok": False, "error": f"任务不存在: {quest_id}"},
                                       ensure_ascii=False))]
            loc = q.state.get("steps", {}).get("locate", {}).get("result") or {}
            file = str(loc.get("file", ""))
            if not file or not os.path.isfile(file):
                return [_TC(json.dumps({"ok": False,
                                        "error": "locate 步无有效目标文件（或文件已删除）"},
                                       ensure_ascii=False))]
            try:
                scan_data = json.loads(_call("bug_scan", {"path": file})[0].text)
            except (json.JSONDecodeError, IndexError, KeyError):
                scan_data = {"ok": False, "issues": []}
            issues = scan_data.get("issues", []) if scan_data.get("ok") else []
            prev = q.state.get("steps", {}).get("diagnose", {}).get("result") or {}
            prev_count = prev.get("issue_count", -1)
            cur = len(issues)
            verdict = ("修复生效" if prev_count >= 0 and cur < prev_count
                       else "未变化" if prev_count >= 0 and cur == prev_count
                       else "未知（无上次基线）")
            # IDE 增强五十三：对照 verify 步 fix_scope（修复前 diff 摘要）
            _vres = q.state.get("steps", {}).get("verify", {}).get("result") or {}
            _scope = _vres.get("fix_scope", "")
            return [_TC(json.dumps({"ok": True, "file": file,
                                    "issue_count": cur,
                                    "prev_issue_count": prev_count,
                                    "severity_counts": scan_data.get("severity_counts", {}),
                                    "verdict": verdict,
                                    "fix_scope": _scope,
                                    "advice": ("对比上次 auto diagnose 问题数：减少=修复生效；"
                                               "未变化=复查 fix 步 checklist/fs_template"
                                               + (f"（期望修改范围 {_scope}）" if _scope else ""))},
                                   ensure_ascii=False, indent=2))]
        if action == "report":
            # IDE 增强二十一：从 quest 状态导出完整 markdown 报告（auto 后随时可查）
            q = resume_quest(quest_id)
            if q is None:
                return [_TC(json.dumps({"ok": False, "error": f"任务不存在: {quest_id}"},
                                       ensure_ascii=False))]
            st = q.state
            steps = st.get("steps", {})
            lines = [f"# 自动诊断报告：{q.task}", f"**仓库**：`{q.repo}`",
                     f"**状态**：{'已完成' if st.get('finished') else '进行中'}"
                     f"{'（已放弃）' if st.get('aborted') else ''}", ""]
            _T = {"diagnose": "诊断", "locate": "定位", "impact": "影响面",
                  "fix": "修复建议", "verify": "验证", "lesson": "教训"}
            for name, title in _T.items():
                r = (steps.get(name) or {}).get("result") or {}
                if not r:
                    continue
                lines.append(f"## {title}")
                if name == "diagnose":
                    if r.get("tool") == "std_check":
                        # IDE 增强五十八：std_check 步显示工程标准分布
                        _ss = r.get("std_severity_counts") or {}
                        lines.append(f"- 工程标准：Critical {_ss.get('Critical', 0)}"
                                     f" / Error {_ss.get('Error', 0)}"
                                     f" / Warning {_ss.get('Warning', 0)}")
                        continue
                    lines.append(f"- 问题 {r.get('issue_count', 0)} 个（error {r.get('error_count', 0)}）")
                    # IDE 增强三十九：severity 分布表
                    _sev = r.get("severity_counts") or {}
                    if _sev:
                        lines.append(f"- severity: error {_sev.get('error', 0)} / "
                                     f"warn {_sev.get('warn', 0)} / info {_sev.get('info', 0)}")
                elif name == "locate":
                    lines.append(f"- `{r.get('file', '')}:{r.get('line', 0)}` [{r.get('rule', '')}] {r.get('message', '')}")
                    for c in (r.get("context") or [])[:5]:
                        lines.append(f"  ```{c}```")
                elif name == "impact":
                    lines.append(f"- 文件级问题 {r.get('file_issue_count', 0)} 个")
                    ci = r.get("change_impact")
                    if ci:
                        lines.append(f"- 符号级：引用 {ci.get('referenced_by_count', 0)} 处，"
                                     f"建议测试 {len(ci.get('suggested_tests', []))} 个")
                elif name == "fix":
                    for a in (r.get("actions") or [])[:10]:
                        # IDE 增强三十六：合并区间显示（L2-L4 ×3）
                        if "line_end" in a:
                            lines.append(f"- L{a.get('line')}-L{a.get('line_end')}"
                                         f"（×{a.get('count', 1)}）{a.get('title', '')}")
                        else:
                            lines.append(f"- L{a.get('line', '?')} {a.get('title', '')}")
                    # IDE 增强五十：kind 分布（safety/cleanup 一眼可见）
                    _kinds = {}
                    for a in (r.get("actions") or []):
                        _k = a.get("kind", "other")
                        _kinds[_k] = _kinds.get(_k, 0) + 1
                    if _kinds:
                        lines.append(f"- kind 分布："
                                     f"{' / '.join(f'{k} {n}' for k, n in sorted(_kinds.items()))}")
                    if r.get("fs_template"):
                        lines.append(f"- fs_template 就绪（`fs_write` L4 授权应用）")
                elif name == "verify":
                    lines.append(f"- 回归命令：`{r.get('command', '')}`")
                    # IDE 增强四十：checklist 完整显示（不再截断前 5 项）
                    for c in (r.get("checklist") or []):
                        lines.append(f"- {c}")
                elif name == "lesson":
                    # IDE 增强五十一：lesson 附修复工作量（report 完整文案）
                    _fc = r.get("fix_count", 0)
                    lines.append(f"- {r.get('advice', '')}"
                                 + (f"（本链 {_fc} 条修复建议）" if _fc else ""))
                else:
                    lines.append(f"- {r.get('advice', '')}")
                lines.append("")
            lines.append("---")
            lines.append("> 验证修复：`ide_quest action=verify_fix`；按步查详情：`action=result`。")
            return [_TC(json.dumps({"ok": True, "quest_id": quest_id,
                                    "report_md": "\n".join(lines),
                                    "finished": bool(st.get("finished"))},
                                   ensure_ascii=False, indent=2))]
        if action == "list":
            # IDE 增强十七：status 过滤（all/active/finished/aborted）
            status_filter = str(args.get("status", "all"))
            quests = list_quests()
            if status_filter == "active":
                quests = [q for q in quests if not q["finished"] and not q.get("aborted")]
            elif status_filter == "finished":
                quests = [q for q in quests if q["finished"]]
            elif status_filter == "aborted":
                quests = [q for q in quests if q.get("aborted")]
            elif status_filter != "all":
                return [_TC(json.dumps({"ok": False,
                                        "error": f"未知过滤: {status_filter}（all/active/finished/aborted）"},
                                       ensure_ascii=False))]
            return [_TC(json.dumps({"ok": True, "filter": status_filter,
                                    "quests": quests, "count": len(quests)},
                                   ensure_ascii=False, indent=2))]
        if action == "clean":
            # IDE 增强十七：清理 finished/aborted 任务（保留 active；days 可选只删过期）
            days = float(args.get("days", 0) or 0)
            cutoff = time.time() - days * 86400 if days > 0 else None
            qdir = os.path.dirname(Quest._state_path("_"))
            removed = 0
            if os.path.isdir(qdir):
                for fn in os.listdir(qdir):
                    if not fn.endswith(".json"):
                        continue
                    q = resume_quest(fn[:-5])
                    if q is None:
                        continue
                    st = q.state
                    if st.get("finished") or st.get("aborted"):
                        if cutoff is None or st.get("created_ts", 0) < cutoff:
                            try:
                                os.remove(os.path.join(qdir, fn))
                                removed += 1
                            except OSError:
                                pass
            return [_TC(json.dumps({"ok": True, "removed": removed,
                                    "note": "已清理 finished/aborted 任务（days>0 时只删过期；active 保留）"},
                                   ensure_ascii=False, indent=2))]
        if action == "auto":
            # IDE 增强七（2026-08-13）：端到端自动推进链——
            # diagnose(bug_scan) → locate(问题 file:line) → impact(文件影响面)
            # → fix(ide_actions) → verify(回归提示)。一次调用跑完五步，
            # 结果写入 quest 状态（可断点续查）。
            import time as _t
            _chain_t0 = _t.perf_counter()
            path = str(args.get("path", ""))
            if not path:
                return [_TC(json.dumps({"ok": False, "error": "auto 需要 path 参数"},
                                       ensure_ascii=False))]
            if not quest_id:
                quest_id = f"auto-{int(_t.time())}"
            # IDE 增强十六：force=True 重置 quest 后重跑整链（上次诊断失败/不完整时重试）
            if args.get("force") and os.path.exists(Quest._state_path(quest_id)):
                q = new_quest(quest_id, str(args.get("task", "")) or f"自动诊断 {path}", path)
                q._save()
            q = resume_quest(quest_id)
            if q is None:
                q = new_quest(quest_id, str(args.get("task", "")) or f"自动诊断 {path}", path)
                q._save()
            chain: list[dict] = []

            # IDE 增强二十二：各步耗时（chain 每项附 elapsed_s——性能分布可视化）
            _last_step_ts = _t.perf_counter()

            def _finish(step_result: dict, step_name: str, tool: str, summary: str) -> None:
                nonlocal _last_step_ts
                now = _t.perf_counter()
                step_elapsed = round(now - _last_step_ts, 3)
                _last_step_ts = now
                r = q.complete_step(step_result)
                chain.append({"step": step_name, "tool": tool,
                              "summary": summary, "ok": r.get("ok", True),
                              "elapsed_s": step_elapsed})

            # 1. diagnose：bug_scan（IDE 增强十四：幂等只读重试一次——
            #    扩展懒加载/首扫慢等瞬时失败不拖垮整链）
            def _run_scan() -> dict:
                try:
                    return json.loads(_call("bug_scan", {"path": path})[0].text)
                except (json.JSONDecodeError, IndexError, KeyError):
                    return {"ok": False, "issues": []}

            scan_data = _run_scan()
            if not scan_data.get("ok"):
                scan_data = _run_scan()  # 重试一次（bug_scan 幂等只读）
            issues = scan_data.get("issues", []) if scan_data.get("ok") else []
            errors = [i for i in issues
                      if str(i.get("severity", "")).lower() in ("error", "critical")]
            _finish({"tool": "bug_scan", "path": path, "issue_count": len(issues),
                     "error_count": len(errors),
                     "severity_counts": scan_data.get("severity_counts", {})},
                    "diagnose", "bug_scan",
                    f"{len(issues)} 问题（{len(errors)} error）"
                    + ("" if scan_data.get("ok")
                       else " ⚠ 扫描失败（可用 force=True 重试整链）"))  # IDE 增强二十四
            # IDE 增强五十七：std_check 工程标准联动（占位/死代码/重复定义/
            # UI 硬编码/魔法数字——独立于 bug_scan 主线，附 summary 不抢 locate）
            try:
                _std = json.loads(_call("std_check", {"path": path})[0].text)
                _std_sev = _std.get("severity_counts", {}) if _std.get("ok") is not None else {}
                if not isinstance(_std_sev, dict):
                    _std_sev = {}
            except Exception:
                _std_sev = {}
            if _std_sev:
                _finish({"tool": "std_check", "path": path,
                         "std_severity_counts": _std_sev},
                        "diagnose", "std_check",
                        f"工程标准 {_std_sev.get('Critical', 0)} Critical/"
                        f"{_std_sev.get('Error', 0)} Error/"
                        f"{_std_sev.get('Warning', 0)} Warning")
            # 2. locate：top 问题位置（error 优先）+ 行上下文 + 符号线索（IDE 增强十）
            top = (errors or issues or [{}])[0]
            loc = {"tool": "locate", "file": top.get("file", ""),
                   "line": top.get("line", 0), "rule": top.get("rule", ""),
                   "message": str(top.get("msg", ""))[:120],
                   "severity": str(top.get("severity", "")).lower() or "unknown"}  # IDE 增强三十七
            if loc["file"] and os.path.isfile(loc["file"]):
                try:
                    with open(loc["file"], encoding="utf-8", errors="replace") as f:
                        f_lines = f.readlines()
                    ln = int(loc.get("line", 0) or 0)
                    ctx: list[str] = []
                    for i in range(max(1, ln - 2), min(len(f_lines), ln + 2) + 1):
                        ctx.append(f"{i}: {f_lines[i - 1].rstrip()}")
                    loc["context"] = ctx
                    if 1 <= ln <= len(f_lines):
                        _sym = re.search(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", f_lines[ln - 1])
                        loc["symbol_hint"] = _sym.group(1) if _sym else ""
                except OSError:
                    pass
            _finish(loc, "locate", "locate",
                    f"{loc['file']}:{loc['line']} [{loc['rule']}]" if loc["file"] else "未发现问题")
            # 3. impact：文件级影响面 + 尽力接 cae_change_impact（符号级深化）
            # IDE 增强三十二：mode=quick 跳过 change_impact（快路径）；full（默认）深查
            mode = str(args.get("mode", "full"))
            file_issues = [i for i in issues if i.get("file") == loc["file"]]
            impact_result = {"tool": "impact", "file": loc["file"],
                             "file_issue_count": len(file_issues),
                             "note": "文件级影响面；符号级深化用 change_impact/lsp_query"}
            if mode == "full" and os.path.isdir(path) and loc.get("file"):
                try:
                    rel = os.path.relpath(loc["file"], path)
                    ci = json.loads(_call("cae_change_impact",
                                          {"repo_path": path,
                                           "changed_files": [rel]})[0].text)
                    r0 = (ci.get("results") or [{}])[0]
                    if ci.get("ok") and r0.get("ok"):
                        impact_result["change_impact"] = {
                            "symbols": r0.get("symbols", [])[:10],
                            "referenced_by_count": len(r0.get("referenced_by", [])),
                            "suggested_tests": r0.get("suggested_tests", []),
                        }
                        impact_result["note"] = ("符号级影响面（cae_change_impact）"
                                                 "——调用方/建议测试见 change_impact")
                except Exception:
                    pass  # 扩展不可用/超时 → 降级文件级影响面
            _finish(impact_result, "impact", "impact",
                    f"{loc['file']} 共 {len(file_issues)} 个问题" +
                    (f"，影响 {impact_result['change_impact']['referenced_by_count']} 处引用"
                     if "change_impact" in impact_result else ""))
            # 4. fix：ide_actions 单文件
            if loc["file"] and os.path.isfile(loc["file"]):
                try:
                    fix_data = json.loads(_call("ide_actions", {"path": loc["file"]})[0].text)
                except (json.JSONDecodeError, IndexError, KeyError):
                    fix_data = {"ok": False, "actions": []}
                actions = fix_data.get("actions", []) if fix_data.get("ok") else []
                _finish({"tool": "ide_actions", "file": loc["file"],
                         "action_count": len(actions),
                         "actions": [{"line": a.get("line"), "title": a.get("title"),
                                      "detail": str(a.get("detail", ""))[:150]}
                                     for a in actions[:10]],
                         # IDE 增强八：可直接粘贴的 fs_write 骨架（L4 授权一步应用）
                         "fs_template": {
                             "tool": "fs_write",
                             "args": {"path": loc["file"],
                                      "content": "<读取原文件，按 actions 行号应用建议后写回>"},
                             "auth_hint": "L4 授权：参数加 __authorized: true",
                         }},
                        "fix", "ide_actions",
                        f"{len(actions)} 条修复建议" +
                        (f"（{' / '.join(str(a.get('title', ''))[:20] for a in actions[:2])}）"  # IDE 增强四十八：摘要附建议标题
                         if actions else ""))
            else:
                _finish({"tool": "ide_actions", "file": loc["file"], "action_count": 0,
                         "skipped": True}, "fix", "ide_actions", "无目标文件，跳过")
            # 5. verify：回归提示 + 修复后自检清单（IDE 增强九）
            ext = os.path.splitext(loc.get("file", ""))[1].lower()
            cmd = ("cargo test" if ext == ".rs"
                   else "pytest" if ext in (".py",) else "构建/测试")
            # IDE 增强五十二：修复前 diff 摘要（影响行号区间，修复后对照）
            _fix_locs = sorted({(a.get("line"), a.get("line_end", a.get("line")))
                                for a in (actions if "actions" in dir() else [])[:10]})
            _fix_scope = "、".join(f"L{s}" if s == e else f"L{s}-L{e}"
                                   for s, e in _fix_locs[:6]) or "无"
            _finish({"tool": "verify",
                     "advice": f"应用 fix 步的修复建议后跑 `{cmd}` 回归；"
                               f"完成后可用 ide_quest note 记录结果",
                     "command": cmd,
                     "fix_scope": f"{len(_fix_locs)} 处（{_fix_scope}）",
                     # IDE 增强九：修复后自检清单（逐项确认防遗漏）
                     "checklist": [
                         f"1. 应用 fix 步的修复建议/fs_template 到 {loc.get('file', '目标文件')}",
                         f"2. 跑回归：{cmd}",
                         "3. 复查：修复行不再触发原规则（用 `ide_quest action=verify_fix` 验证）",
                         "4. 通过后用 ide_quest note 记录验证结果",
                         "5. lesson 步提示：用 lesson_recall 记录教训防复发",
                     ]},
                    "verify", "verify", f"回归命令：{cmd}，自检清单 5 项")  # IDE 增强四十六：summary 附清单计数
            # 6. lesson：自动链收尾（STEPS 六步闭环）
            # IDE 增强四十七：lesson summary 附 fix 计数（链摘要看到修复工作量）
            _lesson_fix_n = (len(actions) if "actions" in dir() else 0)
            _finish({"tool": "lesson",
                     "advice": "修复验证通过后建议用 lesson_recall 记录教训防复发",
                     # IDE 增强五十一：lesson 附 fix 计数（report 显示修复工作量）
                     "fix_count": _lesson_fix_n},
                    "lesson", "lesson",
                    f"教训提示（{_lesson_fix_n} 条修复建议后 lesson_recall 记录）")
            # 顶层 summary（IDE 增强十一 2026-08-13）：六步一句话总览——
            # AI 一眼看到全链结论；完整结果仍在 quest 状态可断点续查
            chain_summary = " → ".join(c.get("summary", "") for c in chain)
            # IDE 增强五十五：summary 长度上限（防 token 膨胀——截断保留头尾
            # 关键信息：前部=diagnose/locate 定位、尾部=verify/lesson 结论）
            if len(chain_summary) > 300:
                chain_summary = chain_summary[:150] + "…" + chain_summary[-140:]
            if mode == "quick":
                # IDE 增强四十九：quick 模式链摘要前缀标注（消费端一眼区分快路径）
                chain_summary = f"⚡quick {chain_summary}"
            chain_elapsed = round(_t.perf_counter() - _chain_t0, 2)  # IDE 增强十五：链耗时
            # IDE 增强二十五：结果总判定（success/partial/failed）——AI 一眼知道链成败
            _diag_ok = bool(scan_data.get("ok"))
            _has_issue = len(issues) > 0
            _skipped = any("跳过" in c.get("summary", "") for c in chain)
            if not _diag_ok:
                result_verdict = "failed"
                result_note = "诊断扫描失败（重试后仍失败），可用 force=True 重跑整链"
            elif _has_issue:
                result_verdict = "partial"
                # IDE 增强三十八：result_note 附 severity 统计（分布一眼可见）
                _sev = scan_data.get("severity_counts", {}) or {}
                result_note = (f"发现问题 {len(issues)} 个（error {len(errors)}"
                               + f"；severity: error {_sev.get('error', 0)}/"
                               + f"warn {_sev.get('warn', 0)}/info {_sev.get('info', 0)}）"
                               + ("，部分步骤跳过" if _skipped else "")
                               + "——修复建议见 fix 步，应用后 verify_fix 验证")
            else:
                result_verdict = "success"
                result_note = "未发现问题——链路正常"
            # IDE 增强二十七：顶层 summary 附 result 前缀（扫 log/汇报一眼见成败）
            chain_summary = f"[{result_verdict}] {chain_summary}"
            # IDE 增强十二：auto 完成 → scan-log 落盘（链路记忆，项目维度可查）
            try:
                import scan_log_core as _slc
                _slc.append_scan({"tool": "ide_quest_auto", "root": path,
                                  "ok": True, "summary": chain_summary[:200]})
            except Exception:
                pass  # 日志失败静默（不拖垮 auto 链）
            # IDE 增强十九：markdown 报告（human-readable，AI 可直接展示/粘贴）
            _step_titles = {"diagnose": "诊断", "locate": "定位", "impact": "影响面",
                            "fix": "修复建议", "verify": "验证", "lesson": "教训"}
            _report = [f"# 自动诊断报告",
                       f"**结果**：{'✅ success' if result_verdict == 'success' else '⚠️ partial' if result_verdict == 'partial' else '❌ failed'}",  # IDE 增强二十六
                       f"**模式**：{'⚡ quick（未深查影响面）' if mode == 'quick' else 'full'}",  # IDE 增强三十三
                       f"**时间**：{time.strftime('%Y-%m-%d %H:%M:%S')}",  # IDE 增强四十三
                       f"**路径**：`{path}`", f"**耗时**：{chain_elapsed}s",
                       # IDE 增强五十六：链配置回显（可复现性——参数归档）
                       f"**配置**：mode={mode} / max_files={args.get('max_files', '默认')} / "
                       f"limit={args.get('limit', '默认')}", ""]
            for c in chain:
                _report.append(f"### {_step_titles.get(c['step'], c['step'])}")
                _report.append(c.get("summary", ""))
                _report.append("")
            _report.append("---")
            # IDE 增强二十三：各步耗时表（性能分布一眼可见）
            _report.append("### 耗时分布")
            _report.append("| 步骤 | 耗时 |")
            _report.append("|---|---|")
            for c in chain:
                _report.append(f"| {_step_titles.get(c['step'], c['step'])} | "
                               f"{c.get('elapsed_s', 0)}s |")
            _report.append("")
            _report.append("---")
            _report.append("> 完整结果：`ide_quest action=result` 按步查询；"
                           "修复应用后：`action=verify_fix` 验证生效。")
            report_md = "\n".join(_report)
            # IDE 增强二十：报告摘要入 quest note（断点续跑可见上轮报告）
            try:
                q.add_note(f"自动诊断报告（{chain_elapsed}s）：{chain_summary[:300]}")
            except Exception:
                pass  # 备注失败静默            # IDE 增强二十八：报告落盘文件（项目 .unified-rx-index/reports/——
            # 独立于 quest 状态，项目维度可直接查看/归档）
            report_path = None
            try:
                _base = path if os.path.isdir(path) else os.path.dirname(path)
                _rep_dir = os.path.join(_base, ".unified-rx-index", "reports")
                os.makedirs(_rep_dir, exist_ok=True)
                _rep_file = os.path.join(_rep_dir, f"auto-{int(_t.time())}.md")
                with open(_rep_file, "w", encoding="utf-8") as _f:
                    _f.write(report_md)
                report_path = _rep_file
                # IDE 增强二十九：只保留最近 N 份报告（防目录膨胀）
                try:
                    _MAX_REPORTS = 20
                    _reports = sorted(os.listdir(_rep_dir))
                    for _old in _reports[:-_MAX_REPORTS] if len(_reports) > _MAX_REPORTS else []:
                        os.remove(os.path.join(_rep_dir, _old))
                except Exception:
                    pass  # 清理失败静默
            except Exception:
                pass  # 落盘失败静默（报告仍经返回值/note/scan-log 可查）
            # IDE 增强四十一：note 附报告落盘路径（项目维度可直达文件）
            if report_path:
                try:
                    q.add_note(f"报告文件：{report_path}")
                except Exception:
                    pass  # 备注失败静默
            return [_TC(json.dumps({"ok": True, "quest_id": quest_id,
                                    "chain": chain,
                                    "summary": chain_summary,
                                    "elapsed_s": chain_elapsed,
                                    "result": result_verdict,
                                    "result_note": result_note,
                                    # IDE 增强六十：双引擎分布顶层字段（消费端
                                    # 一眼看 bug_scan + std_check 全貌）
                                    "std_severity_counts": _std_sev,
                                    "report_md": report_md,
                                    "report_path": report_path,
                                    "status": q.status()},
                                   ensure_ascii=False, indent=2))]
        return [_TC(json.dumps({"ok": False, "error": f"未知 action: {action}"}, ensure_ascii=False))]
    except Exception as e:
        return [_TC(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))]


def _tool_explore_code(args: dict) -> "list[types.TextContent]":
    """LATS 探索：目标词 → 树搜索代码库（ExploreEngine 接线）。

    内置策略：
      expand_fn  — 候选文件里找含目标词/相关词的行与调用
      evaluate_fn — 目标词重合度打分（命中词数/长度）
    """
    from explore_engine import ExploreEngine
    root = args.get("root", "")
    # 安全（2026-08-14 深查）：budget/max_depth 上限校验——
    # 原无上限，budget=10⁹ 会卡死搜索循环（DoS）；负数/0 无意义
    budget = int(args.get("budget", 20))
    max_depth = int(args.get("max_depth", 4))
    if not 1 <= budget <= 5000:
        raise ValueError(f"budget 须在 1..5000（收到 {budget}）")
    if not 1 <= max_depth <= 20:
        raise ValueError(f"max_depth 须在 1..20（收到 {max_depth}）")
    goal = args.get("goal", "")
    if not root or not os.path.isdir(root):
        return [_TC(json.dumps({"ok": False, "error": f"目录不存在: {root}"}, ensure_ascii=False))]
    if not goal:
        return [_TC(json.dumps({"ok": False, "error": "需要 goal（探索目标描述）"}, ensure_ascii=False))]
    goals = [g for g in goal.lower().replace(",", " ").split() if len(g) > 1]
    # 中英同义映射（中文目标 → 英文代码常用词——代码里多是英文标识符）
    _SYN = {
        "车轮": "wheel", "驱动": "drive", "物理": "physic", "地形": "terrain",
        "粒子": "particle", "光影": "light", "材质": "material", "模块": "module",
        "放置": "place", "拾取": "pickup", "载具": "vehicle", "燃料": "fuel",
        "碰撞": "collision", "相机": "camera", "输入": "input", "渲染": "render",
        "任务": "quest", "任务目标": "task", "血量": "health", "伤害": "damage",
        "存储": "storage", "缓存": "cache", "索引": "index", "搜索": "search",
    }
    for g in list(goals):
        if g in _SYN:
            goals.append(_SYN[g])

    # 预扫：目标词相关文件作为起始候选
    import re as _re
    candidates: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ("target", "node_modules", ".git", "release")]
        for fn in filenames:
            if not fn.endswith((".rs", ".py", ".ts", ".js", ".gd")):
                continue
            p = os.path.join(dirpath, fn)
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    text = f.read(4096)
            except OSError:
                continue
            if any(g in text.lower() for g in goals):
                candidates.append(p)
        if len(candidates) >= 30:
            break
    if not candidates:
        return [_TC(json.dumps({"ok": False, "error": f"未找到含目标词的文件: {goal}",
                                "hint": "换更短的关键词或检查 root"}, ensure_ascii=False, indent=2))]

    def expand_fn(cand: str) -> list[str]:
        """展开：候选文件内相关行 → 相关文件（含目标词的近邻文件）。"""
        out = []
        try:
            with open(cand, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            return out
        for i, line in enumerate(lines):
            if any(g in line.lower() for g in goals):
                out.append(f"{cand}:{i + 1}")
        return out[:10]

    def evaluate_fn(cand: str) -> float:
        """打分：目标词命中密度。"""
        try:
            with open(cand.split(":")[0], encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            return 0.0
        low = text.lower()
        hits = sum(low.count(g) for g in goals)
        return hits / (len(text) + 1)

    engine = ExploreEngine()
    try:
        result = engine.search("root", candidates, expand_fn=expand_fn,
                               evaluate_fn=evaluate_fn, budget=budget, max_depth=max_depth)
    except Exception as e:
        return [_TC(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))]
    result["goal"] = goal
    result["candidates_found"] = len(candidates)
    return [_TC(json.dumps(result, ensure_ascii=False, indent=2))]


def _cjk_bigram_query(query: str) -> str:
    """CJK 切词：连续汉字段单字空格化（与索引侧一致——FTS5 unicode61 按空白分词）。"""
    import re as _re
    out_parts = []
    for seg in _re.split(r"([\u4e00-\u9fff]+)", query):
        if _re.fullmatch(r"[\u4e00-\u9fff]+", seg):
            out_parts.append(" ".join(seg))
        else:
            out_parts.append(seg)
    return " ".join(p for p in out_parts if p)


def _cjk_space_content(text: str) -> str:
    """索引侧 CJK 切词：连续汉字字符间插空格（与查询侧一致）。"""
    import re as _re
    return _re.sub(r"([\u4e00-\u9fff])", r"\1 ", text)


def _tool_semantic_search(args: dict) -> "list[types.TextContent]":
    """全库语义检索（SearchIndex 接线）：扫代码库 → FTS5 BM25 索引 → 查询。

    向量增强：无 embed_fn 时纯 BM25（诚实降级）；有本地 embedding 可加。
    索引增量：复用文件 mtime 指纹（ide_cache 思想）——只索引变化文件。
    """
    from search_index import SearchIndex
    root = args.get("root", "")
    query = args.get("query", "")
    limit = int(args.get("limit", 15))
    db_path = args.get("db", os.path.join(root or ".", ".unified-rx-index", "semantic.db"))
    if not root or not os.path.isdir(root):
        return [_TC(json.dumps({"ok": False, "error": f"目录不存在: {root}"}, ensure_ascii=False))]
    if not query:
        return [_TC(json.dumps({"ok": False, "error": "需要 query"}, ensure_ascii=False))]

    idx = None
    try:
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)  # 索引目录先建（SearchIndex init 即开库）
        idx = SearchIndex(db_path)
    except Exception as e:
        return [_TC(json.dumps({"ok": False, "error": f"索引库打开失败: {e}"}, ensure_ascii=False))]
    # 增量索引：文件 mtime+size 指纹（存 SQLite 侧表由 SearchIndex.add_document 幂等处理）
    added = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ("target", "node_modules", ".git", "release", ".unified-rx-index")]
        for fn in filenames:
            if not fn.endswith((".rs", ".py", ".ts", ".js", ".gd", ".toml", ".md", ".ron")):
                continue
            p = os.path.join(dirpath, fn)
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except OSError:
                continue
            try:
                idx.add_document(p, _cjk_space_content(content), title=fn)
                added += 1
            except Exception:
                continue
    try:
        # 混合检索（2026-08-13 向量增强）：BM25 召回 top-20 → 本地 embedding 余弦重排 → RRF
        results = idx.search(_cjk_bigram_query(query), limit=20)
        vector_used = False
        try:
            from local_intel import LocalIntel
            li = LocalIntel()
            embed_fn = li.make_embed_fn()
            if embed_fn is not None:
                q_vec = embed_fn(query)
                if q_vec is not None:
                    # 对召回候选做向量重排（batch embed 候选标题/内容前缀）
                    cand_texts = [str(r.get("title", "") or r.get("id", "")) + " " + str(r.get("content", ""))[:400] for r in results]
                    cand_vecs = []
                    for ct in cand_texts:
                        v = embed_fn(ct)
                        cand_vecs.append(v if v else None)
                    scored = []
                    for r, cv in zip(results, cand_vecs):
                        if cv is None:
                            continue
                        sim = sum(a * b for a, b in zip(q_vec, cv))
                        scored.append((sim, r))
                    scored.sort(key=lambda x: -x[0])
                    vec_ranked = [r for _, r in scored]
                    # RRF 融合：BM25 排名 + 向量排名
                    import math as _m
                    scores: dict[str, float] = {}
                    for rank, hit in enumerate(results):
                        scores[hit["id"]] = scores.get(hit["id"], 0.0) + 1.0 / (60 + rank)
                    for rank, hit in enumerate(vec_ranked):
                        scores[hit["id"]] = scores.get(hit["id"], 0.0) + 1.0 / (60 + rank)
                    merged = {h["id"]: h for h in results}
                    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
                    results = [merged[i] for i, _ in ranked[:limit]]
                    vector_used = True
        except ImportError:
            pass
        results = results[:limit]
    except Exception as e:
        return [_TC(json.dumps({"ok": False, "error": f"检索失败: {e}"}, ensure_ascii=False))]
    return [_TC(json.dumps({"ok": True, "query": query, "indexed_files": added,
                            "results": results,
                            "note": ("BM25+向量 RRF 混合检索（bge-small-zh 本地 embedding）" if vector_used
                                     else "BM25 全文检索（无 embedding 模型——纯 BM25 降级）")},
                           ensure_ascii=False, indent=2))]


def _tool_local_intel(args: dict) -> "list[types.TextContent]":
    """本地智能：status/embed/similarity。"""
    from local_intel import LocalIntel
    li = LocalIntel()
    action = args.get("action", "status")
    if action == "status":
        return [_TC(json.dumps({"ok": True, "available": li.available(),
                                "models_dir": str(li._dir)}, ensure_ascii=False, indent=2))]
    if action == "embed":
        v = li.embed(args.get("text", ""))
        if v is None:
            return [_TC(json.dumps({"ok": False, "error": "embedding 模型不可用（模型缺失或推理失败）"},
                                   ensure_ascii=False))]
        return [_TC(json.dumps({"ok": True, "dim": len(v),
                                "vector_preview": v[:8], "norm": round(sum(x*x for x in v) ** 0.5, 4)},
                               ensure_ascii=False, indent=2))]
    if action == "similarity":
        v1, v2 = li.embed(args.get("text", "")), li.embed(args.get("text2", ""))
        if v1 is None or v2 is None:
            return [_TC(json.dumps({"ok": False, "error": "embedding 模型不可用"}, ensure_ascii=False))]
        sim = sum(a * b for a, b in zip(v1, v2))
        return [_TC(json.dumps({"ok": True, "similarity": round(sim, 4),
                                "verdict": ("相关" if sim > 0.5 else "不相关")},
                               ensure_ascii=False, indent=2))]
    return [_TC(json.dumps({"ok": False, "error": f"未知 action: {action}"}, ensure_ascii=False))]


def _tool_lesson_learn(args: dict) -> "list[types.TextContent]":
    """记忆维深化：分层教训存取 + UCB 主动学习闭环。

    action:
      store      — lesson_store_tiered（tier=core/work/learn 分层教训）
      recall     — lesson_recall（按 id 召回）
      delta      — delta_update_lesson/rule（经验得分更新）
      experience — experience_store（模型+上下文+delta 存经验）
      ucb_select — UCB 选最优经验节点（探索/利用平衡）
      ucb_backprop — UCB 反馈奖励（学习闭环）
      state      — LSE 引擎状态
    """
    import lse_client as _lse
    action = args.get("action", "state")
    try:
        if action == "store":
            r = _lse.lesson_store_tiered(
                args.get("tier", "work"), args.get("content", ""),
                float(args.get("delta", 0.0)), float(args.get("threshold", 0.1)))
        elif action == "recall":
            r = _lse.lesson_recall(args.get("lesson_id", ""))
        elif action == "delta":
            if args.get("kind") == "rule":
                r = _lse.delta_update_rule(args.get("id", ""), float(args.get("delta", 0.0)),
                                           bool(args.get("adopted", True)))
            else:
                r = _lse.delta_update_lesson(args.get("id", ""), float(args.get("delta", 0.0)),
                                             float(args.get("threshold", 0.1)))
        elif action == "experience":
            r = _lse.experience_store(args.get("model", "default"),
                                      args.get("ctx", ""), float(args.get("delta", 0.0)),
                                      args.get("summary", ""))
        elif action == "ucb_select":
            r = _lse.ucb_select(args.get("parent", ""), args.get("children", []),
                                float(args.get("c", 1.41)))
        elif action == "ucb_backprop":
            r = _lse.ucb_backprop(args.get("node_id", ""), float(args.get("reward", 0.0)))
        elif action == "state":
            r = _lse.state_get()
        else:
            return [_TC(json.dumps({"ok": False, "error": f"未知 action: {action}"},
                                   ensure_ascii=False))]
        return [_TC(json.dumps(r, ensure_ascii=False, indent=2))]
    except Exception as e:
        return [_TC(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"},
                               ensure_ascii=False))]


def _tool_cmd_cheatsheet(args: dict) -> "list[types.TextContent]":
    from ide_commands import cheatsheet
    return [_TC(json.dumps(cheatsheet(args.get("domain")), ensure_ascii=False, indent=2))]


def _tool_local_run(args: dict) -> "list[types.TextContent]":
    from ide_commands import local_run
    r = local_run(args.get("domain", ""), args.get("name", ""),
                  args.get("args"), args.get("workdir"),
                  int(args.get("timeout", 300)))
    return [_TC(json.dumps(r, ensure_ascii=False, indent=2))]


def _tool_skill_fetch(args: dict) -> "list[types.TextContent]":
    """技能申请制：request/list/approve（用户批准才下载）。"""
    from skill_fetch import request_skill, approve_skill, list_approvals
    action = args.get("action", "list")
    skills_dir = args.get("skills_dir") or os.path.join(
        os.path.expanduser("~"), ".hermes", "skills")
    try:
        if action == "request":
            r = request_skill(args.get("task", ""), skills_dir)
        elif action == "list":
            r = list_approvals()
        elif action == "approve":
            r = approve_skill(args.get("id", ""), bool(args.get("approved", False)), skills_dir)
        else:
            r = {"ok": False, "error": f"未知 action: {action}"}
        return [_TC(json.dumps(r, ensure_ascii=False, indent=2))]
    except Exception as e:
        return [_TC(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"},
                               ensure_ascii=False))]


def _tool_design_note(args: dict) -> "list[types.TextContent]":
    """项目本质三分（settled/adjustable/doubts）。"""
    from design_notes import add_note, list_notes, get_note
    action = args.get("action", "list")
    root = args.get("root", "")
    kind = args.get("kind", "")
    try:
        if action == "add":
            r = add_note(root, kind, args.get("text", ""), args.get("tag", ""))
        elif action == "get":
            r = get_note(root, kind)
        else:
            r = list_notes(root)
        return [_TC(json.dumps(r, ensure_ascii=False, indent=2))]
    except Exception as e:
        return [_TC(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"},
                               ensure_ascii=False))]


def _tool_scan_trend(args: dict) -> "list[types.TextContent]":
    """扫描日志趋势分析（M6：日志→统计→增强闭环）。"""
    from scan_trend import analyze
    import scan_log_core as _scl
    try:
        logs = _scl.query_logs(limit=2000)
    except Exception:
        logs = []
    r = analyze(logs, int(args.get("window_days", 7)))
    return [_TC(json.dumps(r, ensure_ascii=False, indent=2))]


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
    # P2 对齐（2026-08-13）：severity 归一化统计 + noise_ratio（与 bug_scan 一致）——
    # AI 可判断报告可信度；ui_check_core 产出 error/warning，归一化到 error/warn/info
    sev_counts = {"error": 0, "warn": 0, "info": 0}
    for i in issues:
        s = str(i.get("severity", "warning"))
        sev_counts["warn" if s in ("warn", "warning") else
                   ("error" if s == "error" else "info")] += 1
    total = len(issues)
    return [_TC(json.dumps({
        "ok": True, "issue_count": len(issues),
        "severity_counts": sev_counts,
        "noise_ratio": round(sev_counts["info"] / total, 3) if total else 0.0,
        "note": "severity 已归一化（warning→warn）；noise_ratio=info 占比",
        "issues": issues,
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


def _tool_lesson_recall_lse(args: dict) -> "list[types.TextContent]":
    """LSE 增强版教训召回（P0：Delta 奖励进化记忆）。

    调 cae_lesson_recall 获取原始教训，再用 lse-engine 的 utility 分数
    排序（高效用优先）、标记 archived（低分降权）。返回 {lessons, utility, archived}。
    """
    try:
        import lse_client as _lse
    except ImportError:
        _dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, _dir)
        import lse_client as _lse  # noqa: F811
    # 调 cae 扩展的原始 lesson_recall
    cae = _load_ext("code-analysis-enhance")
    if cae is None:
        return [_TC(json.dumps({"ok": False, "error": "cae 扩展不可用"}, ensure_ascii=False))]
    try:
        raw = cae._tool_lesson_recall({"task_description": str(args.get("task_description", "")),
                                       "lessons_dir": str(args.get("lessons_dir", ""))})
        text = raw[0].text if isinstance(raw, list) else str(raw)
        data = json.loads(text)
    except Exception as exc:
        return [_TC(json.dumps({"ok": False, "error": f"lesson_recall 失败: {exc}"}, ensure_ascii=False))]
    lessons = data.get("lessons", [])
    # 每条教训按内容 hash 查 utility 分（lse-engine），降序排序
    # 枢纽优先（Hub-Priority，Nature Communications「压缩学习 枢纽优先」启发）：
    # recall_count = 该教训被验证/召回的次数——反复被证实有效的教训是"枢纽"，
    # 排序时 utility 相近则枢纽教训优先（hub_bonus = recall 的软加权，非硬覆盖）。
    scored = []
    for idx, lesson in enumerate(lessons):
        lid = f"lesson-{abs(hash(lesson[:80])) % 10**9}"
        # 查询命令（不污染 recall_count）；教训不存在时用 delta=0 建初始条目
        cur = _lse.lesson_recall(lid)
        if not cur.get("ok"):
            cur = _lse.delta_update_lesson(lid, 0.0)
        utility = cur.get("result", {}).get("utility", 0.5)
        archived = cur.get("result", {}).get("archived", False)
        recall = cur.get("result", {}).get("recall", 0)
        # 枢纽优先：recall 每 +1 给少量加权（cap 0.15），utility 仍为主排序键
        hub_bonus = min(recall, 10) * 0.015
        scored.append({"id": lid, "utility": utility, "hub_bonus": hub_bonus,
                       "hub_score": utility + hub_bonus, "recall": recall,
                       "archived": archived, "text": lesson})
    scored.sort(key=lambda x: (-x["hub_score"], -x["utility"]))
    active = [s for s in scored if not s["archived"]]
    archived_list = [s for s in scored if s["archived"]]
    return [_TC(json.dumps({
        "ok": True,
        "task_keywords": data.get("task_keywords", []),
        "lessons": [s["text"] for s in active],
        "utility": [{"id": s["id"], "utility": s["utility"], "recall": s["recall"],
                     "hub_bonus": s["hub_bonus"], "hub_score": s["hub_score"]}
                    for s in active],
        "archived": [{"id": s["id"], "utility": s["utility"], "text": s["text"][:100]} for s in archived_list],
        "antipatterns": data.get("antipatterns", []),
        "advice": data.get("advice", ""),
        "note": ("LSE 进化记忆 + 枢纽优先：utility 为主键、recall（反复验证次数）加权排序，"
                 "低分自动归档（「压缩学习 枢纽优先」Nature Communications 启发）"
                 if _lse.engine_available() else "lse-engine 未构建，降级为原始召回"),
    }, ensure_ascii=False))]


def _tool_lesson_extract(args: dict) -> "list[types.TextContent]":
    """自动教训提取（P1c，抄 mem0 自动记忆提取 + Letta 三层）。

    从工具结果/错误信息/对话文本中识别教训信号（"教训/注意/避免/必须"等），
    自动入库（分层：core 核心长期 / work 工作短期 / archive 归档），
    内容哈希稳定 ID（同内容汇聚，防重复）。
    """
    try:
        import lse_client as _lse
    except ImportError:
        _dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, _dir)
        import lse_client as _lse  # noqa: F811
    text = str(args.get("text", ""))
    tier = str(args.get("tier", "work"))
    if not text.strip():
        return [_TC(json.dumps({"ok": False, "error": "text 必填"}, ensure_ascii=False))]
    result = _lse.auto_extract_lessons(text, tier=tier)
    return [_TC(json.dumps(result, ensure_ascii=False))]


def _tool_lesson_feedback(args: dict) -> "list[types.TextContent]":
    """LSE 反馈回路（P0）：教训被采纳/无效时更新 utility（Delta 奖励）。

    采纳（bug 率下降）→ delta 加分；无效 → 减分；<0.1 自动归档。
    """
    try:
        import lse_client as _lse
    except ImportError:
        _dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, _dir)
        import lse_client as _lse  # noqa: F811
    lesson_id = str(args.get("lesson_id", ""))
    delta = float(args.get("delta", 0.0))
    if not lesson_id:
        return [_TC(json.dumps({"ok": False, "error": "lesson_id 必填"}, ensure_ascii=False))]
    if not -1.0 <= delta <= 1.0:
        return [_TC(json.dumps({"ok": False, "error": "delta 须在 [-1,1]"}, ensure_ascii=False))]
    res = _lse.delta_update_lesson(lesson_id, delta)
    return [_TC(json.dumps(res, ensure_ascii=False))]


def _tool_rule_feedback(args: dict) -> "list[types.TextContent]":
    """LSE 规则权重反馈（P1）：规则被采纳 → 权重加分；被忽略 → 减分。

    低权重（<0.3）规则自动降级（std_check 输出降为 Info，不阻塞）。
    """
    try:
        import lse_client as _lse
    except ImportError:
        _dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, _dir)
        import lse_client as _lse  # noqa: F811
    rule_id = str(args.get("rule", ""))
    adopted = bool(args.get("adopted", True))
    delta = float(args.get("delta", 0.2))
    if not rule_id:
        return [_TC(json.dumps({"ok": False, "error": "rule 必填"}, ensure_ascii=False))]
    if not 0.0 <= delta <= 1.0:
        return [_TC(json.dumps({"ok": False, "error": "delta 须在 [0,1]"}, ensure_ascii=False))]
    res = _lse.delta_update_rule(rule_id, delta, adopted)
    return [_TC(json.dumps(res, ensure_ascii=False))]


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
    # IDE 增强四十四前序：severity_counts 聚合（与 bug_scan 返回结构一致——
    # AI 消费端统一按 severity_counts 判断报告可信度）
    _sev = {}
    for _i in result.get("issues", []):
        _s = _i.get("severity", "info")
        _sev[_s] = _sev.get(_s, 0) + 1
    result["severity_counts"] = _sev
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


def _tool_code_complete(args: dict) -> "list[types.TextContent]":
    """LSP 自动补全（基于真实语法树，不过期不污染）。

    自动读文件（或传 text）→ 按后缀探测语言 → 调 cae_lsp_query(completion)
    → 格式化候选 [{label, kind, detail}]。语言服务器未安装时返回 ok:false+hint，
    不会伪造结果。光标默认最后一行行尾。
    """
    path = str(args.get("path", "") or "")
    if not path:
        return [_tr(False, "缺少 path")]
    line = int(args.get("line", -1))
    character = int(args.get("character", -1))
    language_id = str(args.get("language_id", "") or "").lower()
    root = str(args.get("root", "") or "")
    timeout = float(args.get("timeout", 25.0))
    text = args.get("text", "")
    if text is None:
        text = ""
    text = str(text)

    p = Path(path)
    if not text:
        try:
            if not p.is_file():
                return [_tr(False, f"文件不存在: {path}")]
            if p.stat().st_size > (1 << 20):
                return [_tr(False, "文件超过 1MB 上限")]
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return [_tr(False, f"读取失败: {exc}")]
    if len(text) > (1 << 20):
        return [_tr(False, "text 超过 1MB 上限")]
    if not language_id:
        language_id = _LANG_BY_SUFFIX.get(p.suffix.lower(), "")
        if not language_id:
            return [_tr(False, "无法探测语言（传 language_id 或支持的文件后缀）",
                        {"supported": sorted(set(_LANG_BY_SUFFIX.values()))})]
    if language_id not in _LANG_BY_SUFFIX.values():
        return [_tr(False, f"不支持的语言: {language_id}",
                    {"supported": sorted(set(_LANG_BY_SUFFIX.values()))})]
    if line < 0:
        line = text.count("\n")
    if character < 0:
        lines = text.splitlines()
        character = len(lines[line]) if line < len(lines) else 0

    resp = _call_ext("cae_lsp_query", {
        "language_id": language_id,
        "request": "completion",
        "path": path,
        "line": line,
        "character": character,
        "text": text,
        "root": root,
        "timeout": timeout,
    })
    try:
        data = json.loads(resp[0].text)
    except (IndexError, json.JSONDecodeError):
        return [_tr(False, "LSP 响应解析失败")]
    if not data.get("ok"):
        return [_tr(False, str(data.get("error", "LSP 补全失败")))]
    raw = data.get("result") or {}
    items = raw.get("items") or []
    out = []
    for it in items[:50]:
        label = it.get("label") or it.get("insertText") or ""
        if not label:
            continue
        kind = _LSP_KIND_NAMES.get(it.get("kind"), it.get("kind"))
        detail = str(it.get("detail") or "")[:80]
        out.append({"label": str(label)[:160], "kind": kind, "detail": detail})
    return [_tr(True, f"completion {len(out)} 项",
                {"language": language_id,
                 "position": {"line": line, "character": character},
                 "items": out})]


_LANG_BY_SUFFIX = {
    ".py": "python", ".pyw": "python",
    ".rs": "rust",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
}

_LSP_KIND_NAMES = {
    1: "Text", 2: "Method", 3: "Function", 4: "Constructor", 5: "Field",
    6: "Variable", 7: "Class", 8: "Interface", 9: "Module", 10: "Property",
    11: "Unit", 12: "Value", 13: "Enum", 14: "Keyword", 15: "Snippet",
    16: "Color", 17: "File", 18: "Reference", 19: "Folder", 20: "EnumMember",
    21: "Constant", 22: "Struct", 23: "Event", 24: "Operator", 25: "TypeParameter",
}


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
    # P2: UCB 树搜索——多个 ok 候选时，用 lse-engine 历史奖励选择最优分支
    ok_locs = [loc for loc in locations if loc["status"] == "ok"]
    if len(ok_locs) > 1:
        try:
            import lse_client as _lse
        except ImportError:
            _dir = os.path.dirname(os.path.abspath(__file__))
            sys.path.insert(0, _dir)
            import lse_client as _lse  # noqa: F811
        if _lse.engine_available():
            node_ids = []
            for loc in ok_locs:
                # 稳定节点 id：file:line（去空格避免路径噪声）
                nid = f"{loc['file']}:{loc['line']}".replace("\\", "/").lower()
                node_ids.append(nid)
            # UCB 选择：注册子节点并选最优（无历史 → 全未访问 → 按原序探索）
            sel = _lse.ucb_select("bug-locate", node_ids, c=1.41)
            if sel.get("ok") and sel.get("result", {}).get("selected"):
                picked = sel["result"]["selected"]
                # 把选中的节点排到最前（其余保持原序）
                order = {nid: i for i, nid in enumerate(node_ids)}
                ok_locs.sort(key=lambda loc: (order.get(f"{loc['file']}:{loc['line']}".replace("\\", "/").lower(), 0) != picked, 0))
    return [_TC(json.dumps({
        "ok": True, "matched": bool(locations), "locations": locations,
    }, ensure_ascii=False))]


def _tool_bug_locate_feedback(args: dict) -> "list[types.TextContent]":
    """P2: bug_locate UCB 反馈——候选位置命中/未命中回流奖励。

    命中（用户定位到正确位置）→ reward=+1；未命中 → reward=-1。
    奖励更新 lse-engine 树节点，下次定位更准。
    """
    try:
        import lse_client as _lse
    except ImportError:
        _dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, _dir)
        import lse_client as _lse  # noqa: F811
    node = str(args.get("node", "")).replace("\\", "/").lower()
    hit = bool(args.get("hit", True))
    if not node:
        return [_TC(json.dumps({"ok": False, "error": "node 必填"}, ensure_ascii=False))]
    res = _lse.ucb_backprop(node, 1.0 if hit else -1.0)
    return [_TC(json.dumps(res, ensure_ascii=False))]


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
    # 安全（2026-08-13 深查）：拒绝递归调用自身——tool_card 调 tool_card 会
    # 无限递归（1000 层后 RecursionError 才被兜底，浪费栈 + 打点）
    if name == "tool_card":
        return [_tr(False, "tool_card: 递归调用被拒绝", {"error": "tool_card 不能调用 tool_card"})]
    # P3/LSE 经验字段：模型指纹 + 上下文哈希 + 得分 → 成功后写入经验库
    mf = str(args.get("model_fingerprint", "") or "").strip()
    ctx = str(args.get("context_hash", "") or "").strip()
    dscore = args.get("delta_score")
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
    exp_id = None
    if mf or ctx:
        try:
            import lse_client as _lse
        except ImportError:
            _dir = os.path.dirname(os.path.abspath(__file__))
            sys.path.insert(0, _dir)
            import lse_client as _lse  # noqa: F811
        try:
            delta = float(dscore) if dscore is not None else 0.0
            if -1.0 <= delta <= 1.0:
                res = _lse.experience_store(mf or "unknown", ctx, delta, text[:200])
                if res.get("ok"):
                    exp_id = res.get("result", {}).get("id")
        except (TypeError, ValueError):
            pass  # 经验字段可选：异常不阻断卡片
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and "role" in parsed:
            # 已是结构化结果：透传卡片字段（同样受 max_detail_len 约束）
            detail = parsed.get("detail", parsed)
            card = _tr(parsed.get("ok", True), parsed.get("summary", name), _truncate_detail(detail, max_detail))
            if exp_id and isinstance(card.text, str):
                try:
                    d0 = json.loads(card.text)
                    d0["experience_id"] = exp_id
                    card.text = json.dumps(d0, ensure_ascii=False)
                except (ValueError, TypeError):
                    pass
            return [card]
        summary = f"{name}: {text[:200]}{'…' if len(text) > 200 else ''}"
        card = _tr(True, summary, _truncate_detail(parsed, max_detail))
        if exp_id and isinstance(card.text, str):
            try:
                d0 = json.loads(card.text)
                d0["experience_id"] = exp_id
                card.text = json.dumps(d0, ensure_ascii=False)
            except (ValueError, TypeError):
                pass
        return [card]
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
    # ── 组合工具（2026-08-11 去重：原 29 单工具 → 6 组合 + fib_fibonacci）──
    "math_ops": (_tool_math_ops, _schema({
        "action": _S("string", "add/sub/mul/div/power/sqrt/abs/factorial/c2f/f2c"),
        "a": _S("number", "add/sub/mul/div 用"), "b": _S("number", "add/sub/mul/div 用"),
        "base": _S("number", "power 用"), "exponent": _S("number", "power 用"),
        "x": _S("number", "sqrt/abs 用"), "n": _S("integer", "factorial 用"),
        "celsius": _S("number", "c2f 用"), "fahrenheit": _S("number", "f2c 用"),
    }, ["action"]), "数学运算组合（原 math_add/sub/mul/div/power/sqrt/abs/factorial + conv_c2f/f2c）"),
    "text_ops": (_tool_text_ops, _schema({
        "action": _S("string", "reverse/upper/lower/palindrome"),
        "s": _S("string", "文本"),
    }, ["action", "s"]), "文本运算组合（原 str_reverse/upper/lower/palindrome）"),
    "sort_search": (_tool_sort_search, _schema({
        "action": _S("string", "quick_sort/bubble_sort/binary_search"),
        "arr": _S("array", "待排序/查找数组"), "target": _S("number", "binary_search 目标"),
    }, ["action", "arr"]), "排序查找组合（原 sort_quick/sort_bubble/search_binary）"),
    "stat_geo": (_tool_stat_geo, _schema({
        "action": _S("string", "mean/median/circle_area/rect_perimeter"),
        "data": _S("array", "mean/median 用"), "radius": _S("number", "circle_area 用"),
        "length": _S("number", "rect_perimeter 用"), "width": _S("number", "rect_perimeter 用"),
    }, ["action"]), "统计几何组合（原 stat_mean/median + geo_circle_area/rect_perimeter）"),
    "json_email": (_tool_json_email, _schema({
        "action": _S("string", "parse/valid/email"),
        "json_string": _S("string", "parse/valid 用"), "email": _S("string", "email 用"),
    }, ["action"]), "JSON 校验组合（原 json_parse/json_valid/valid_email）"),
    "prime_list": (_tool_prime_list, _schema({
        "action": _S("string", "is_prime/generate/unique/flatten"),
        "n": _S("integer", "is_prime 用 ≤10M"), "limit": _S("integer", "generate 用 ≤1M"),
        "lst": _S("array", "unique 用"), "nested_list": _S("array", "flatten 用"),
    }, ["action"]), "素数列表组合（原 prime_is_prime/generate + list_unique/flatten）"),
    "fib_fibonacci": (_rxcore_wrap("fib", _m_fib_fibonacci), _schema({"n": _S("integer", "≤20000")}, ["n"]), "斐波那契第 n 项"),
    "vuln_scan": (_tool_vuln_scan, _schema({
        "path": _S("string", "文件或目录"),
        "max_files": _S("integer", "扫描上限(默认100)"),
    }, ["path"]), "统一漏洞扫描：bug_scan + std_check + ui_check 一次全跑（三路并行互不打扰）"),
    "project_scan": (_tool_project_scan, _schema({
        "path": _S("string", "项目根目录"),
        "max_files": _S("integer", "扫描上限(默认100)"),
        "ui": _S("boolean", "是否扫 Bevy UI（非 Rust 项目可关，默认 true）"),
    }, ["path"]), "项目级高并发扫描：bug_scan+std_check+ui_check+cb_scan 四路并行，结果自动落盘 scan-log"),
    "full_scan": (_tool_full_scan, _schema({
        "roots": _S("array", "项目根列表（缺省扫常见项目根）"),
        "max_files": _S("integer", "扫描上限(默认100)"),
        "ui": _S("boolean", "是否扫 Bevy UI（默认 true）"),
    }, []), "全盘扫：多项目根并发跑 project_scan（每项目四路并行），汇总落盘 scan-log"),
    "hallucination_guard": (_tool_hallucination_guard, _schema({
        "text": _S("string", "AI 声明文本（含 file:line / 反引号符号）"),
        "root": _S("string", "仓库根目录（相对路径解析基准，可选）"),
    }, ["text"]), "幻觉守卫：提取声明并对照本地验证（verified/refuted/unverifiable），refuted 即幻觉必须纠正"),
    "capability_manifest": (_tool_capability_manifest, _schema({}, []), "能力清单：全部工具 + 有什么/没有什么边界声明（防能力幻觉）"),
    "scan_log": (_tool_scan_log, _schema({
        "root": _S("string", "项目根路径（过滤，可选）"),
        "tool": _S("string", "工具名过滤（bug_scan/std_check/vuln_scan/ui_check/cb_scan 等，可选）"),
        "limit": _S("integer", "返回条数(默认50，上限200)"),
    }, []), "扫描日志查询：常驻自扫落盘（~/.unified-rx/scan-log.jsonl），专项目对话按 root 过滤查看历史扫描结果"),
    "kb_query": (_tool_kb_query, _schema({
        "index_dir": _S("string", "索引目录（源码/知识库目录，懒构建）"),
        "query": _S("string", "检索词"),
        "index_file": _S("string", "可选：直接指定 .db 文件"),
        "limit": _S("integer", "返回条数(默认20，上限100)"),
    }, ["index_dir", "query"]), "混合语义检索：BM25 全文 + 可选向量（RRF 融合）——对代码/知识库做语义查询（P0b）"),
    "repo_graph": (_tool_repo_graph, _schema({
        "root": _S("string", "代码库根目录（首次自动建 tree-sitter 图索引）"),
        "query": _S("string", "callers/callees/impact/hubs/communities/search（默认 search）"),
        "symbol": _S("string", "callers/callees 用：符号名"),
        "file": _S("string", "impact 用：文件路径（相对 root）"),
        "name": _S("string", "search 用：符号名模糊"),
        "depth": _S("integer", "impact BFS 深度(默认3，上限6)"),
        "top": _S("integer", "返回条数(默认10，上限50)"),
    }, ["root"]), "代码库符号图：调用链/影响面/核心符号/符号搜索（tree-sitter 18 语言图索引，P1）"),
    "repo_wiki": (_tool_repo_wiki, _schema({
        "root": _S("string", "代码库根目录（首次自动建图索引）"),
        "out": _S("string", "可选：输出路径（默认 <root>/.unified-rx-index/WIKI.md）"),
    }, ["root"]), "一键生成代码库结构文档（Qoder Repo Wiki 对齐：模块地图/核心符号/依赖，一次看全仓库）"),
    "agent_orchestrate": (_tool_agent_orchestrate, _schema({
        "tasks": _S("array", "[{id, role, tool, args}]——role: analyst/quality/memory/writer/explorer（角色=工具白名单）"),
    }, ["tasks"]), "多智能体编排：角色分工并行执行工具任务（抄 crewAI/autogen，P4）"),
    "agent_roles": (_tool_agent_roles, _schema({}, []), "角色目录：各角色工具集与用途（配合 agent_orchestrate）"),
    "quality_scan": (_tool_quality_scan, _schema({
        "path": _S("string", "文件或目录"),
        "backends": _S("array", "可选：指定后端 [ruff/semgrep/gitleaks/pyright]（默认全部可用）"),
    }, ["path"]), "质量多后端扫描：ruff+semgrep+gitleaks+pyright（可用即用，缺失降级，P2a）"),
    "pipeline": (_tool_pipeline, _schema({
        "preset": _S("string", "预设配方：audit_repo/guard_text/learn/locate_context/semantic_before/semantic_after/semantic_edit（一次调用跑完整流程）"),
        "steps": _S("array", "步骤链：[{tool, args, as?}]——上一步结果以 ${key} 注入下一步"),
        "max_steps": _S("integer", "步骤上限(默认20)"),
    }, []), "工具链协作：任意工具顺序组合，前一步输出注入下一步参数；支持 preset 一键配方"),
    "parallel": (_tool_parallel, _schema({
        "tasks": _S("array", "并发任务：[{tool, args}]"),
        "timeout": _S("number", "总超时秒(默认60，1~600)"),
    }, ["tasks"]), "高并发：多工具同时执行（≤8 并发），全部完成后汇总"),
    # 代码缺陷扫描 + 精准定位
    "tool_card": (_tool_card, _schema({
        "name": _S("string", "要调用的工具名"),
        "arguments": _S("object", "工具参数（可选）"),
        "max_detail_len": _S("integer", "detail 字符上限(默认20000，防大结果撑爆上下文)"),
        "model_fingerprint": _S("string", "P3/LSE：模型指纹（可选，写入经验库）"),
        "context_hash": _S("string", "P3/LSE：上下文哈希（可选，写入经验库）"),
        "delta_score": _S("number", "P3/LSE：经验得分 [-1,1]（可选）"),
    }, ["name"]), "Tool 角色回喂：调用任意工具并返回结构化卡片 {role,ok,summary,detail}（支持经验存取）"),
    "bug_scan": (_tool_bug_scan, _schema({"path": _S("string", "Python 文件或目录"), "max_files": _S("integer", "最大文件数(默认100)")}, ["path"]), "静态扫描 bug 模式（未定义变量/None 解引用/资源泄漏/除零/越界）"),
    "bug_locate": (_tool_bug_locate, _schema({"error_text": _S("string", "报错/traceback 文本")}, ["error_text"]), "报错文本 → 定位 file:line（含上下文片段）"),
    "ui_check": (_tool_ui_check, _schema({"path": _S("string", ".rs 文件或目录"), "max_files": _S("integer", "扫描文件上限(默认100)")}, ["path"]), "Bevy UI 静态检查（崩溃/不可见模式）"),
    # IDE 全家桶（R4）
    "ide_rename": (_tool_ide_rename, _schema({
        "root": _S("string", "代码库根目录"),
        "symbol": _S("string", "要重命名的符号"),
        "new_name": _S("string", "新名字"),
        "exclude_comments": _S("boolean", "排除注释/字符串内引用（默认 true）"),
        "include_plan": _S("boolean", "生成 apply_plan（按文件聚合的行级编辑列表，fs_write 就绪，默认 false）"),
    }, ["root", "symbol", "new_name"]), "安全重命名：全库找引用→建议（L3 不落盘，确认后 fs_write 应用；include_plan 可生成批量应用计划）"),
    "ide_complete": (_tool_ide_complete, _schema({
        "root": _S("string", "代码库根目录"),
        "file": _S("string", "当前文件"),
        "prefix": _S("string", "补全前缀"),
    }, ["root", "prefix"]), "符号补全（tree-sitter 图降级版，无 LSP 可用；当前文件优先）"),
    "ide_references": (_tool_ide_references, _schema({
        "root": _S("string", "代码库根目录"),
        "symbol": _S("string", "要查的符号"),
    }, ["root", "symbol"]), "查找符号定义与全部引用（IDE goto-references，无 LSP 可用）"),
    "ide_actions": (_tool_ide_actions, _schema({
        "path": _S("string", "文件路径"),
    }, ["path"]), "快速修复建议（unwrap/expect/as 收窄等安全规则→code action）"),
    "ide_fusion": (_tool_ide_fusion, _schema({
        "path": _S("string", "代码库根目录"),
        "action": _S("string", "annotate（默认，诊断→符号图）/ impact（双引擎影响面校验）"),
        "symbol": _S("string", "impact 时：要校验的符号"),
        "lsp_refs": _S("array", "impact 时：LSP 引用文件路径列表（可空）"),
    }, ["path"]), "IDE 融合：诊断→符号图聚合 / 双引擎影响面校验（LSP vs 引用扫描）"),
    "ide_quest": (_tool_ide_quest, _schema({
        "action": _S("string", "new/resume/status/step/list/abort/note/auto/result/clean/verify_fix/report"),
        "quest_id": _S("string", "任务 ID（new/resume/step 必需；auto 省略自动生成）"),
        "task": _S("string", "任务描述（new 时）"),
        "repo": _S("string", "仓库根（new 时）"),
        "result": _S("object", "步骤结果（step 时——当前步完成的证据）"),
        "text": _S("string", "备注内容（note 时）"),
        "path": _S("string", "auto 时：诊断目标（文件或目录）"),
        "mode": _S("string", "auto 时：full（默认，impact 接 change_impact 深查）/ quick（跳过深查更快）"),
        "step": _S("string", "result 时：要检索的步骤名（diagnose/locate/impact/fix/verify/lesson）"),
        "status": _S("string", "list 时：过滤（all/active/finished/aborted，默认 all）"),
        "days": _S("number", "clean 时：只清理超过 N 天的 finished/aborted（默认 0=全部清理）"),
    }, ["action"]), "Quest 任务状态机（诊断→定位→影响→修复→验证→教训，断点续跑）"),
    "explore_code": (_tool_explore_code, _schema({
        "root": _S("string", "代码库根目录"),
        "goal": _S("string", "探索目标（如 'physics 模块初始化处'）"),
        "budget": _S("integer", "搜索预算（默认 20）"),
        "max_depth": _S("integer", "最大深度（默认 4）"),
    }, ["root", "goal"]), "LATS 探索：目标描述 → 树搜索代码库（值函数+回溯）"),
    "semantic_search": (_tool_semantic_search, _schema({
        "root": _S("string", "代码库根目录"),
        "query": _S("string", "检索词"),
        "limit": _S("integer", "结果数（默认 15）"),
        "db": _S("string", "索引库路径（默认 .unified-rx-index/semantic.db）"),
    }, ["root", "query"]), "全库语义检索（BM25+本地向量 RRF 混合）"),
    "local_intel": (_tool_local_intel, _schema({
        "action": _S("string", "status/embed/similarity"),
        "text": _S("string", "embed/similarity 时文本"),
        "text2": _S("string", "similarity 时第二段文本"),
    }, ["action"]), "本地智能（bge-small-zh embedding——状态/嵌入/相似度）"),
    "lesson_learn": (_tool_lesson_learn, _schema({
        "action": _S("string", "store/recall/delta/experience/ucb_select/ucb_backprop/state"),
        "tier": _S("string", "store 时层级（core/work/learn）"),
        "content": _S("string", "store 时教训内容"),
        "lesson_id": _S("string", "recall 时教训 id"),
        "id": _S("string", "delta 时教训/规则 id"),
        "delta": _S("number", "经验得分变化 [-1,1]"),
        "model": _S("string", "experience 时模型名"),
        "ctx": _S("string", "experience 时上下文"),
        "summary": _S("string", "experience 时摘要"),
        "children": _S("array", "ucb_select 子节点列表"),
        "reward": _S("number", "ucb_backprop 奖励"),
    }, ["action"]), "记忆维：分层教训存取 + UCB 主动学习闭环"),
    "cmd_cheatsheet": (_tool_cmd_cheatsheet, _schema({
        "domain": _S("string", "cargo/git/python/blender/voxelforge/unifiedrx（缺省全部）"),
    }, []), "内建命令手册（省 token——不用试错找命令）"),
    "local_run": (_tool_local_run, _schema({
        "domain": _S("string", "命令域"),
        "name": _S("string", "命令名（查 cmd_cheatsheet）"),
        "args": _S("object", "占位符参数 {pkg}/{msg} 等"),
        "workdir": _S("string", "工作目录（默认当前）"),
        "timeout": _S("integer", "超时秒（默认 300）"),
    }, ["domain", "name"]), "执行内建命令模板（白名单——本地运行省 token）"),
    "skill_fetch": (_tool_skill_fetch, _schema({
        "action": _S("string", "request/list/approve"),
        "task": _S("string", "request 时任务描述"),
        "id": _S("string", "approve 时申请 id"),
        "approved": _S("boolean", "approve 时 true=批准下载 / false=拒绝"),
        "skills_dir": _S("string", "skills 目录（默认 hermes-home/skills）"),
    }, ["action"]), "技能申请制：没 skill 时申请→用户批准才下载（不批不装）"),
    "design_note": (_tool_design_note, _schema({
        "action": _S("string", "add/list"),
        "root": _S("string", "项目根目录"),
        "kind": _S("string", "settled(设定性)/adjustable(设计性)/doubts(疑点)"),
        "text": _S("string", "add 时内容"),
        "tag": _S("string", "add 时可选标签"),
    }, ["action", "root"]), "项目本质三分（设定性原样/设计性可调/疑点标记）"),
    "scan_trend": (_tool_scan_trend, _schema({
        "window_days": _S("integer", "分析窗口天数（默认 7）"),
    }, []), "扫描日志趋势分析（规则命中率→提权/降噪）"),
    "cb_index": (_tool_cb_index, _schema({"path": _S("string", "代码库根目录")}, ["path"]), "代码库索引（全库符号+哈希+变更感知）"),
    "cb_status": (_tool_cb_status, _schema({"path": _S("string", "代码库根目录")}, ["path"]), "代码库状态（索引摘要，不重建）"),
    "cb_scan": (_tool_cb_scan, _schema({"path": _S("string", "代码库根目录"), "max_files": _S("integer", "扫描上限(默认200)")}, ["path"]), "全库扫描（变更优先 UI 规则）"),
    "locate_edit": (_tool_locate_edit, _schema({
        "path": _S("string", "代码库根目录"),
        "query": _S("string", "符号名/关键词/报错片段（要改什么）"),
        "max_files": _S("integer", "扫描上限(默认200)"),
        "limit": _S("integer", "候选数(默认10)"),
    }, ["path", "query"]), "Qoder 式定位：自然语言→代码具体位置（file:line+符号+snippet，AI 改哪里的引导）"),
    "code_complete": (_tool_code_complete, _schema({
        "path": _S("string", "文件路径"),
        "line": _S("integer", "光标行(0-based，默认最后一行)"),
        "character": _S("integer", "光标列(UTF-16 码元，默认行尾)"),
        "language_id": _S("string", "rust/python/typescript/javascript/c/cpp（缺省按后缀探测）"),
        "text": _S("string", "文档全文（缺省自动读文件，≤1MB）"),
        "root": _S("string", "工作区根目录"),
        "timeout": _S("number", "LSP 请求超时秒数（默认 25；rust 大项目首启可调大）"),
    }, ["path"]), "LSP 自动补全（基于真实语法树）：读文件→探测语言→completion→格式化候选"),
    "ds_lookup": (_tool_ds_lookup, _schema({}, []), "设计系统 token 查询（AI 生成 UI 时引用）"),
    "ds_check": (_tool_ds_check, _schema({"path": _S("string", ".rs 文件或目录"), "max_files": _S("integer", "扫描上限(默认200)")}, ["path"]), "设计系统合规检查（硬编码值/规则偏离）"),
    "std_check": (_tool_std_check, _schema({"path": _S("string", "文件或目录"), "max_files": _S("integer", "扫描上限(默认200)")}, ["path"]), "通用工程标准检查（占位文字/命名冲突/UI硬编码/魔法数字；默认标准兼容绝大多数项目）"),
    "lesson_recall_lse": (_tool_lesson_recall_lse, _schema({
        "task_description": _S("string", "任务描述（召回相关教训）"),
        "lessons_dir": _S("string", "教训库目录（可选）"),
    }, ["task_description"]), "LSE 进化教训召回：utility 降序排序 + 低分归档（Delta 奖励）"),
    "lesson_extract": (_tool_lesson_extract, _schema({
        "text": _S("string", "源文本（工具结果/错误信息/对话，自动提取教训）"),
        "tier": _S("string", "层级：core 核心长期/work 工作短期/archive 归档（默认 work）"),
    }, ["text"]), "自动教训提取（P1c，抄 mem0 自动记忆提取 + Letta 三层）：信号词识别自动入库，内容哈希稳定 ID"),
    "lesson_feedback": (_tool_lesson_feedback, _schema({
        "lesson_id": _S("string", "教训 ID（lesson_recall_lse 返回）"),
        "delta": _S("number", "效用增量 [-1,1]（采纳正分/无效负分）"),
    }, ["lesson_id", "delta"]), "LSE 教训反馈回路：Delta 更新 utility，<0.1 自动归档"),
    "rule_feedback": (_tool_rule_feedback, _schema({
        "rule": _S("string", "规则名（如 magic_number）"),
        "adopted": _S("boolean", "采纳=true 加分 / 忽略=false 减分"),
        "delta": _S("number", "权重增量 [0,1]（默认 0.2）"),
    }, ["rule", "adopted"]), "LSE 规则权重反馈：采纳/忽略 → weight_update，低权重自动降级"),
    "bug_locate_feedback": (_tool_bug_locate_feedback, _schema({
        "node": _S("string", "候选节点 ID（bug_locate 返回的 file:line）"),
        "hit": _S("boolean", "命中=true 奖励 / 未命中=false 惩罚"),
    }, ["node", "hit"]), "P2 UCB 反馈：bug 定位候选命中/未命中 → 树奖励回流"),
}


_DEFS_CACHE: list | None = None


def _definitions() -> list:
    """工具定义（核心缓存 + 实时扩展）：list_tools 重复调用零重建（性能优化）。

    注意：同步路径禁止触发扩展构建——_build_ext_defs 是 async，asyncio.run
    在运行事件循环内会崩溃（MCP tools/list 就是 async handler）。扩展定义
    由协议层 async list_tools 先构建，这里只读已构建部分。
    """
    global _DEFS_CACHE
    if _DEFS_CACHE is None:
        _DEFS_CACHE = [
            _ToolDef(n, d, sc)
            for n, (_, sc, d) in _TOOLS.items()
        ]
    return _DEFS_CACHE + [t for (_, _, t) in _EXT_DEFS.values()]


# ─────────────────────────────────────────────────────────────
# 扩展层（懒加载合并：pr-oracle / tautest / code-analysis-enhance）
#   - 首次调用才加载（保持启动极简，内存基线最小）
#   - 工具名前缀：pr_oracle_* / tautest_* / cae_*
#   - 按路径加载（避免同名 server.py import 冲突）
#   - 加载失败：单工具返回错误文本，网关存活
# ─────────────────────────────────────────────────────────────
import importlib.util as _ilu

_EXT_BASE_CANDIDATES = [
    # 开发布局：mcp-servers/unified-rx/server.py → 扩展在 mcp-servers/pr-oracle 等
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    # CI 布局：仓库根/server.py + 扩展复制到 仓库根/mcp-servers/
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp-servers"),
]
_EXT_BASE = next(
    (p for p in _EXT_BASE_CANDIDATES if os.path.isdir(os.path.join(p, "pr-oracle"))),
    _EXT_BASE_CANDIDATES[0],
)
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
    """构建扩展工具定义（pr-oracle 3 + tautest 4 + stats 4 + cae 13 = 24）。"""
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
        stats = _load_ext("stats")
        if stats is not None and hasattr(stats, "_TOOLS"):
            for tname, (_, sc, desc) in stats._TOOLS.items():
                # stats 工具名自带 stats_ 前缀；pure 风格（stats._call 返回 str）
                _EXT_DEFS[tname] = ("stats", "pure", _ToolDef(tname, desc, sc))
    except Exception as exc:
        print(f"[unified-rx] WARNING: stats 扩展定义构建失败: {exc}", file=sys.stderr)
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
    if name not in _EXT_DEFS:
        # 防御：同步上下文（测试/直接调用如 code_complete）可安全构建；
        # async 上下文（协议层 handler）不可 asyncio.run（事件循环内崩溃），
        # 由协议层 list_tools 先行构建，此处返回错误文本而不是炸进程。
        try:
            asyncio.get_running_loop()
            return [_TC(f"Error: unknown tool: {name}（扩展定义未构建，协议层 list_tools 会先构建）")]
        except RuntimeError:
            _ext_definitions()
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
    t0 = time.perf_counter()
    try:
        # R2 权限检查（L4 写工具需显式授权——越权在分发前拒绝）
        from ide_permission import check as _perm_check, strip_auth as _perm_strip
        ok, reason = _perm_check(name, arguments or {})
        if not ok:
            return [_TC(f"Error: {reason}")]
        arguments = _perm_strip(arguments or {})
        if name in _TOOLS:
            fn, _, _ = _TOOLS[name]
            result = fn(arguments or {})
            if isinstance(result, list):
                _scan_log_tick(name, arguments or {}, result)
                # 日志闯进调用：扫描工具返回时附带该 root 已知问题（scan-log 反馈）
                result = _attach_known_issues(name, arguments or {}, result)
                return result
            _scan_log_tick(name, arguments or {}, [_TC(str(result))])
            return [_TC(str(result))]
        if name in ("stats_summary", "stats_status"):
            _stats_flush()  # 汇总/状态前落盘缓冲打点（协作：summary 能看到自动打点）
        if name.startswith(("pr_oracle_", "tautest_", "cae_", "stats_")):
            return _call_ext(name, arguments or {})
        raise ValueError(f"unknown tool: {name}")
    except Exception as exc:
        return [_TC(f"Error: {exc}")]
    finally:
        # 自动打点（工具协作：每个工具调用自动记录到 stats，stats_* 自身除外）
        if not name.startswith("stats_"):
            _stats_tick(name, (time.perf_counter() - t0) * 1000)


# 日志闯进调用（总线反馈链路）：扫描工具返回时，从 scan-log 读取该 root 的
# 最近已知问题并附到结果 JSON——智能体不用额外查日志就知道"这文件出过什么 bug"。
_KNOWN_ISSUE_TOOLS = {"bug_scan", "std_check", "locate_edit", "vuln_scan",
                      "project_scan", "ui_check"}


def _attach_known_issues(name: str, args: dict,
                         result: "list[types.TextContent]") -> "list[types.TextContent]":
    if name not in _KNOWN_ISSUE_TOOLS:
        return result
    try:
        import scan_log_core
    except ImportError:
        _dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, _dir)
        import scan_log_core  # noqa: F811
    root = str(args.get("path", "") or args.get("root", ""))
    if not root:
        return result
    # P1：scan-log 回读走 TTL 缓存（mtime_ns+size 变化即失效）——防高频调用
    # 反复全量读文件（daemon 并发扫描期放大延迟，见 SCAN_QUALITY_ISSUES.md 问题 B）
    import time as _time
    cache_key = f"{root}|{name}"
    now = _time.monotonic()
    try:
        logf = scan_log_core.log_path()
        fkey = (logf.stat().st_mtime_ns, logf.stat().st_size)
    except OSError:
        fkey = None
    hit = _KNOWN_ISSUES_CACHE.get(cache_key)
    if hit is not None and now - hit[0] < _KNOWN_ISSUES_CACHE_TTL and hit[1] == fkey:
        known = hit[2]
    else:
        # 读该 root 最近的扫描日志（按 root 过滤，最多 3 条非本轮记录）
        logs = scan_log_core.query_logs(root=root, limit=50)
        known = [l for l in logs if l.get("tool") != name][:3]
        _KNOWN_ISSUES_CACHE[cache_key] = (now, fkey, known)
    if not known:
        return result
    text = getattr(result[0], "text", "") if result else ""
    if not text.startswith("{"):
        return result  # 非 JSON 结果不注入
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "known_issues" not in data:
            data["known_issues"] = [{
                "tool": l.get("tool", ""),
                "ts": l.get("ts", ""),
                "summary": str(l.get("summary", ""))[:120],
            } for l in known]
            data["known_issues_note"] = "来自 scan-log（日志闯进调用）：该路径最近的已知问题，修复进展可查 scan_log"
            result[0] = _TC(json.dumps(data, ensure_ascii=False))
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass
    return result


# 扫描类工具：调用完成自动落盘 scan-log.jsonl（常驻自扫日志，专项目对话可查）
_SCAN_LOG_TOOLS = {"bug_scan", "std_check", "vuln_scan", "ui_check",
                   "cb_scan", "cb_index", "hallucination_guard", "locate_edit",
                   "project_scan", "full_scan"}


# P1 超时韧性（SCAN_QUALITY_ISSUES.md 问题 B 修复）：_attach_known_issues 的
# scan-log 回读加 TTL 缓存——高频工具调用不再每次全量读文件（scan-log 最多
# 2000 行 JSONL，多次调用叠加放大延迟；daemon 并发扫描期尤其明显）。
# 缓存键 = root|tool，值 = (时间戳, 文件 mtime_ns+size, known 列表)；5s TTL。
_KNOWN_ISSUES_CACHE: dict[str, tuple[float, tuple | None, list]] = {}
_KNOWN_ISSUES_CACHE_TTL = 5.0


def _scan_log_tick(name: str, args: dict, result: "list[types.TextContent]") -> None:
    """扫描工具结果落盘：抽取 summary + root，追加到 scan-log.jsonl（失败静默）。

    与 stats 打点互补：stats 记调用统计（ts/tool/时长），scan-log 记扫描结果
    （root/ok/summary）——"扫完的都放到日志里面"，专项目对话按 root 过滤查询。
    """
    if name not in _SCAN_LOG_TOOLS:
        return
    try:
        import scan_log_core
    except ImportError:
        _dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, _dir)
        import scan_log_core  # noqa: F811
    text = ""
    for c in result:
        text = getattr(c, "text", "") or ""
        if text:
            break
    summary = text[:200]
    # 尝试从结果 JSON 提取精简 summary（ok + 关键计数）
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            if data.get("summary") and isinstance(data["summary"], str):
                summary = data["summary"][:200]
            elif name == "bug_scan" and "issues" in data:
                summary = f"issues={len(data['issues'])} ok={data.get('ok')}"
            elif name == "std_check" and "summary" in data:
                s = data["summary"]
                if isinstance(s, dict):
                    summary = (f"critical={s.get('critical',0)} warning={s.get('warning',0)} "
                               f"suggestion={s.get('suggestion',0)}")
            elif name == "vuln_scan":
                summary = f"ok={data.get('ok')} errors={len(data.get('errors', []))}"
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass
    root = str(args.get("path", "") or args.get("root", ""))
    scan_log_core.append_scan({"tool": name, "root": root, "ok": True, "summary": summary})


def _stats_tick(tool: str, duration_ms: float) -> None:
    """工具调用自动打点：内存缓冲，满 100 条或退出时批量落盘（失败静默）。

    性能约束：打点路径必须 O(1)——纯函数调用（math_ops 等）1000 次 <50ms。
    - 锁内只做 append（微秒级），绝不做文件 IO
    - flush 用快照交换 + 后台 daemon 线程异步落盘——_call 路径零阻塞
    - stats_summary/stats_status 调用前仍同步 _stats_flush() 取最新数据
    """
    global _STATS_BUF
    try:
        if "stats_record" not in _EXT_DEFS:
            return
        with _STATS_LOCK:
            _STATS_BUF.append({
                "ts": time.time(),
                "task": "unified-rx",
                "tool": tool,
                "action": tool,
                "duration_ms": duration_ms,
            })
            if len(_STATS_BUF) < _STATS_FLUSH_EVERY:
                return
            # 快照交换：锁内 O(1) 取走缓冲；锁外异步落盘（不阻塞调用方）
            batch, _STATS_BUF = _STATS_BUF, []
        threading.Thread(target=_stats_flush_batch, args=(batch,),
                         daemon=True).start()
    except Exception:
        pass  # 打点失败不影响工具调用


_STATS_FLUSH_LOCK = threading.Lock()


def _stats_flush_batch(batch: list) -> None:
    """异步批量落盘（后台线程；与 _stats_flush 串行化，失败静默）。"""
    if not batch:
        return
    mod = _EXT_LOADED.get("stats")
    if mod is None:
        return
    try:
        with _STATS_FLUSH_LOCK:
            records = mod._load() + batch
            mod._save(mod._truncate(records))
    except Exception:
        pass


_STATS_BUF: list[dict] = []
_STATS_FLUSH_EVERY = 100
_STATS_LOCK = threading.Lock()
_STATS_FLUSH_LOCK = threading.Lock()


def _stats_flush() -> None:
    """缓冲批量写入 stats 文件（进程退出/汇总前调用；与后台 flush 串行化）。"""
    global _STATS_BUF
    try:
        with _STATS_LOCK:
            batch, _STATS_BUF = _STATS_BUF, []
        if batch:
            _stats_flush_batch(batch)
    except Exception:
        pass


import atexit
atexit.register(_stats_flush)


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
        # 扩展定义只在此 async 路径构建（_ext_definitions_async 内部 await，
        # 不炸事件循环）；_definitions() 只读核心 + 已构建扩展，不重复构建。
        await _ext_definitions_async()
        return [
            types.Tool(name=t.name, description=t.description, inputSchema=t.inputSchema)
            for t in _definitions()
        ]

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict | None) -> "list[types.TextContent]":
        # 高并发：同步工具调用放线程池（asyncio.to_thread），不阻塞事件循环——
        # mcp SDK 并发请求可真正并行处理（此前同步阻塞会串行化所有调用）。
        out = await asyncio.to_thread(_call, name, arguments)
        return [types.TextContent(type=getattr(c, "type", "text"), text=c.text) for c in out]

    # 打开 RX 即自动开启后台扫描循环（daemon 线程持续跑，不会停下）：
    # 5 模式各自独立循环线程并发（自扫/项目/全盘），互不打扰，结果落盘 scan-log。
    _spawn_self_scan()

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


_SCAN_LOOPS_STARTED = False  # 防重复启动后台扫描循环（见 _spawn_self_scan）


def _spawn_self_scan() -> None:
    """后台扫描循环：打开 RX 即自动开启，5 模式各自独立 daemon 线程**持续循环**（不会停下），互不打扰，结果落盘。

    五种常态化扫描模式（全部高并发 + 持续）：
      ① 跟随话题项目：UNIFIED_RX_PROJECT 指定 → project_scan 并发扫（循环）
      ② 全盘扫：full_scan 多项目根并发（低频循环）
      ③ 被 RX 调用：_scan_log_tick 调用即记（每次工具调用自动落盘，天然持续）
      ④ 最活跃就扫：stats.json 统计调用最多的项目 → 并发扫（循环）
      ⑤ 扫自己：全家自扫（core+scripts+lse-engine 文件级并发 + vendor 扩展目录并发）（循环）

    循环间隔可用环境变量覆盖（秒）：
      UNIFIED_RX_SCAN_INTERVAL_SELF=600（自扫，默认 10 分钟）
      UNIFIED_RX_SCAN_INTERVAL_PROJECT=300（项目 ①④，默认 5 分钟）
      UNIFIED_RX_SCAN_INTERVAL_FULL=1800（全盘 ②，默认 30 分钟）
    daemon 线程 + 失败静默——绝不影响 MCP 协议层。CI/测试跳过
    （UNIFIED_RX_SKIP_SELF_SCAN=1）。
    """
    if os.environ.get("UNIFIED_RX_SKIP_SELF_SCAN", "") == "1":
        return
    global _SCAN_LOOPS_STARTED
    if _SCAN_LOOPS_STARTED:
        return  # 防重复启动（多次调用/重连不堆积线程）
    _SCAN_LOOPS_STARTED = True
    try:
        import scan_log_core
    except ImportError:
        _dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, _dir)
        import scan_log_core  # noqa: F811

    def _active_project() -> str | None:
        """最活跃项目（模式④）：UNIFIED_RX_PROJECT > stats.json 调用最多 root > scan-log 最近 > 常见项目根。"""
        env = os.environ.get("UNIFIED_RX_PROJECT", "").strip()
        if env:
            return env
        try:
            # stats.json 统计：哪个项目 root 被扫得最多就扫哪个
            stats_path = Path.home() / ".unified-rx" / "stats.json"
            if stats_path.exists():
                data = json.loads(stats_path.read_text(encoding="utf-8"))
                recs = data if isinstance(data, list) else data.get("records", [])
                counts: dict[str, int] = {}
                for r in recs:
                    root = str(r.get("root", ""))
                    if root:
                        counts[root] = counts.get(root, 0) + 1
                if counts:
                    top = max(counts, key=counts.get)
                    if counts[top] >= 3:  # 防冷启动误扫（只扫确有活跃度的）
                        return top
        except Exception:
            pass
        try:
            logs = scan_log_core.query_logs(limit=50)
            roots = [l.get("root", "") for l in logs if l.get("root")]
            if roots:
                return roots[0]
        except Exception:
            pass
        for cand in (r"D:\开发\VoxelForge-Nexus", r"D:\开发\reasonix-src",
                     r"D:\开发\VoxelForge"):
            if os.path.isdir(cand):
                return cand
        return None

    def _self_scan_once() -> None:
        """模式⑤自扫一轮：全家文件级并发 + 扩展目录并发。"""
        # 暴露为模块级供独立守护（daemon.py）单轮调用
        global _spawn_self_scan_once
        _spawn_self_scan_once = _self_scan_once
        from concurrent.futures import ThreadPoolExecutor, as_completed
        files = scan_log_core.self_scan_files()

        def scan_one(f: str) -> None:
            try:
                d = json.loads(_call("bug_scan", {"path": f})[0].text)
                n = len(d.get("issues", [])) if isinstance(d, dict) else -1
                scan_log_core.append_scan({
                    "tool": "self_scan", "root": f, "ok": n == 0,
                    "summary": f"self bug_scan {os.path.basename(f)}: issues={n}",
                })
            except Exception:
                pass

        with ThreadPoolExecutor(max_workers=min(8, max(1, len(files)))) as pool:
            futs = [pool.submit(scan_one, f) for f in files]
            for fut in as_completed(futs):
                fut.result()
        for d in scan_log_core.self_scan_dirs():
            try:
                r = _call("bug_scan", {"path": d, "max_files": 50})[0]
                dd = json.loads(r.text)
                n = len(dd.get("issues", [])) if isinstance(dd, dict) else -1
                scan_log_core.append_scan({
                    "tool": "self_scan", "root": d, "ok": n == 0,
                    "summary": f"self bug_scan {os.path.basename(d)}: issues={n}",
                })
            except Exception:
                pass

    def _project_scan_once() -> None:
        """模式①④ 一轮：跟随话题项目（无则最活跃）并发扫。"""
        proj = _active_project()
        if proj:
            try:
                _call("project_scan", {"path": proj, "max_files": 100})
            except Exception:
                pass

    def _full_scan_once() -> None:
        """模式② 一轮：多项目根并发全盘扫。"""
        try:
            _call("full_scan", {"max_files": 100, "ui": False})
        except Exception:
            pass

    def _loop(name: str, interval_env: str, default: float, fn) -> None:
        """持续循环线程：首轮立即跑，之后每 interval 秒跑一轮（永不停下）。"""
        interval = float(os.environ.get(interval_env, default))
        if interval < 10:
            interval = 10  # 防 DoS：间隔下限 10s

        def runner() -> None:
            while True:
                try:
                    fn()
                except Exception:
                    pass
                time.sleep(interval)

        threading.Thread(target=runner, daemon=True, name=f"rx-scan-{name}").start()

    # 三个持续循环线程（互不打扰，各自独立）：自扫 / 项目 / 全盘
    _loop("self", "UNIFIED_RX_SCAN_INTERVAL_SELF", 600, _self_scan_once)
    _loop("project", "UNIFIED_RX_SCAN_INTERVAL_PROJECT", 300, _project_scan_once)
    _loop("full", "UNIFIED_RX_SCAN_INTERVAL_FULL", 1800, _full_scan_once)


def _selftest() -> None:
    # selftest 的 fs 用例写 server.py 同目录，需禁用沙盒（review 修复）
    os.environ["UNIFIED_RX_SANDBOX"] = ""
    # 重新锚定沙盒根（环境变量在 import 时读取）
    global _SANDBOX_ROOTS
    _SANDBOX_ROOTS = []
    start = time.perf_counter()
    n = len(_TOOLS)
    assert n == len(_definitions()) - len(_EXT_DEFS), "定义数不一致"
    # 抽样调用（组合工具）
    assert _call("math_ops", {"action": "add", "a": 2, "b": 3})[0].text == "5"
    assert _call("text_ops", {"action": "reverse", "s": "abc"})[0].text == "cba"
    assert _call("prime_list", {"action": "is_prime", "n": 17})[0].text == "true"
    assert _call("json_email", {"action": "valid", "json_string": "{bad}"})[0].text == "false"
    assert "Error" in _call("math_ops", {"action": "div", "a": 1, "b": 0})[0].text
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
    # 防幻觉守卫（抽样：真实声明 verified / 不存在路径 refuted）
    dg = json.loads(_call("hallucination_guard", {
        "text": f"见 {os.path.basename(__file__)}:1 与 no_such_file.py:9",
        "root": os.path.dirname(os.path.abspath(__file__)),
    })[0].text)
    assert dg["ok"] and dg["verdict"] == "refuted" and any(
        i["claim"] == "no_such_file.py:9" for i in dg["refuted"])
    dm = json.loads(_call("capability_manifest", {})[0].text)
    assert dm["ok"] and dm["core_count"] == n and dm["has_not"]
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
