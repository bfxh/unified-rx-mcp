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
        # IDE 增强 179：缺失键 → 空串（原样返回 `${key}` 字面量会污染
        # 工具参数——如 rules="${rules}" 会把字面量当规则名过滤）
        return ctx.get(args[2:-1], "")
    return args


# pipeline 预设配方（一次调用 = 多步流程，减少 AI 工具调用轮次）
# 每个配方返回步骤列表；${path} 等由调用方参数注入。
_PIPELINE_PRESETS: dict[str, list[dict]] = {
    # 仓库审计：索引 → 状态 → 漏洞 → 工程标准（4 步 1 次调用）
    "audit_repo": [
        # IDE 增强 199：首步 cb_index（构建/更新索引——未索引时一键审计
        # 也能直接可用，不再返回 indexed=False）
        {"tool": "cb_index", "args": {"path": "${path}"}, "as": "index"},
        {"tool": "bug_scan", "args": {"path": "${path}", "max_files": 100,
                                      "rules": "${rules}"}, "as": "bugs"},
        {"tool": "std_check", "args": {"path": "${path}", "max_files": 100,
                                       "rules": "${rules}"}, "as": "std"},
        {"tool": "vuln_scan", "args": {"path": "${path}", "max_files": 100,
                                       "rules": "${rules}"}, "as": "vuln"},
    ],
    # 挖漏洞默认链（2026-08-13 M1）：生产危险规则 → Python bug → 质量后端 → 符号聚合
    # 智能体做任何改动默认先跑——不用问用户"要不要扫描"
    "bug_hunt": [
        {"tool": "rust_scan", "args": {"path": "${path}"}, "as": "panic"},
        {"tool": "bug_scan", "args": {"path": "${path}", "max_files": 100,
                                      "rules": "${rules}"}, "as": "bugs"},
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
    _t0 = time.perf_counter()
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
                       "context_keys": sorted(ctx),
                       # IDE 增强 244：管线耗时（ms——聚合收官）
                       "elapsed_ms": round((time.perf_counter() - _t0) * 1000, 1)},
                      ensure_ascii=False)


# ── 高并发优化（2026-08-14 用户点名"出事了高并发出大问题"）──
# 共享线程池 + 全局并发闸门：嵌套 ThreadPoolExecutor（parallel 8 路 ×
# project_scan 4 路 × vuln_scan 3 路）每调用新建池——32 线程风暴实测
# 嵌套放大 7.3x（线程 churn + GIL 争抢）——共享池消除创建风暴，闸门防爆炸。
_SHARED_POOL: "object | None" = None  # 惰性创建（首次并发调用时）
_CONCURRENCY_SEM = threading.Semaphore(24)  # 全局并发上限（防嵌套爆炸）


def _pool() -> "object":
    """共享线程池（惰性创建；daemon 线程——不阻塞进程退出）。"""
    global _SHARED_POOL
    if _SHARED_POOL is None:
        from concurrent.futures import ThreadPoolExecutor
        _SHARED_POOL = ThreadPoolExecutor(
            max_workers=16, thread_name_prefix="rx-shared")
    return _SHARED_POOL


_SHARED_PROC_POOL: "object | None" = None  # 共享进程池（惰性创建）
_PROC_POOL_LOCK = threading.Lock()


def _proc_pool() -> "object":
    """共享进程池（全局单例——并发 bug_scan 复用同一池，避免每次调用
    新建 8×N 子进程的 spawn 风暴；Windows spawn 下 import server 开销大，
    复用后首次创建只付一次成本）。"""
    global _SHARED_PROC_POOL
    if _SHARED_PROC_POOL is None:
        with _PROC_POOL_LOCK:
            if _SHARED_PROC_POOL is None:
                from concurrent.futures import ProcessPoolExecutor
                _SHARED_PROC_POOL = ProcessPoolExecutor(
                    max_workers=min(8, os.cpu_count() or 2))
    return _SHARED_PROC_POOL


def _tool_parallel(args: dict) -> str:
    """并发组：多工具同时执行（共享池 ≤16 并发——嵌套不再新建池），全部完成后汇总。"""
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

    with _CONCURRENCY_SEM:  # 全局闸门：防嵌套爆炸（24 上限）
        _pool()  # 确保池已创建
        futs = [_pool().submit(run, i, t) for i, t in enumerate(tasks)]
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
    # IDE 增强 169（安全）：index_dir/index_file 沙盒校验——防任意目录
    # .db 读取（信息泄露）/越界索引构建
    index_dir = _check_path(index_dir)
    if index_file:
        index_file = _check_path(index_file)
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
        # 懒建索引（2026-08-14 补实现——文档宣称"首次调用自动建索引"但
        # 从未实现：db 空时只建空库，search 恒 0 命中。实测复现后补齐：
        # 从 index_dir 扫描代码/文档文件 add_many 建索引）
        if idx.stats()["docs"] == 0:
            _docs = []
            for _dp, _dns, _fns in os.walk(index_dir):
                _dns[:] = [d for d in _dns if d not in
                           ("target", "node_modules", ".git", "release",
                            ".unified-rx-index", "__pycache__")]
                for _fn in _fns:
                    if not _fn.lower().endswith(tuple(_ANALYZE_EXTS)):
                        continue
                    _fp = os.path.join(_dp, _fn)
                    try:
                        with open(_fp, encoding="utf-8", errors="replace") as _f:
                            _content = _f.read()
                    except OSError:
                        continue
                    if _content.strip():
                        _docs.append({"id": os.path.relpath(_fp, index_dir),
                                      "content": _content, "title": _fn})
            if _docs:
                idx.add_many(_docs)
        _t0 = time.perf_counter()
        hits = idx.search_hybrid(query, embed_fn=None, limit=limit)
        _ms = round((time.perf_counter() - _t0) * 1000, 1)
        _hits_langs: dict[str, int] = {}
        for _h in hits:
            _sfx = os.path.splitext(str(_h.get("id", "")))[1].lower().lstrip(".")
            if _sfx:
                _hits_langs[_sfx] = _hits_langs.get(_sfx, 0) + 1
        return json.dumps({
            "ok": True, "query": query, "count": len(hits), "db": db,
            # IDE 增强 215：检索耗时（ms——性能可见）
            "elapsed_ms": _ms,
            "hits": [{"id": h["id"], "title": h.get("title", ""),
                      "meta": h.get("meta", {}),
                      "snippet": (h.get("content") or "")[:200]} for h in hits],
            "note": "BM25 全文检索（向量路未配置时自动降级；配置 embed_fn 后启用 RRF 融合）",
            # IDE 增强 198：索引规模（docs 数——检索覆盖面一眼可见）
            "indexed_docs": idx.stats().get("docs", 0),
            # IDE 增强 150：命中质量提示（0 命中时引导换词/查索引）
            "advice": (f"命中 {len(hits)} 条（前 3："
                       f"{', '.join(str(h.get('title', ''))[:20] for h in hits[:3])}）"
                       if hits else "0 命中——换关键词，或确认索引目录含代码/文档文件"),
            # IDE 增强 290：检索命中语言分布（hits 文件后缀——AI 知道
            # 相关代码的语言）
            "languages": dict(sorted(_hits_langs.items(), key=lambda kv: -kv[1])),
        }, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({"ok": False, "error": f"检索失败: {exc}"}, ensure_ascii=False)


# ── P1a/P1b 掌握引擎（2026-08-12：tree-sitter 符号图，抄 codebase-memory 图查询）──
def _timed_json(fn):
    """IDE 增强 245：JSON 返回工具的耗时注入装饰器（通用——多分支
    返回工具一次覆盖；repo_graph 先启用，其他工具按需）。"""

    def _wrapped(*a, **kw):
        _t0 = time.perf_counter()
        r = fn(*a, **kw)
        try:
            d = json.loads(r)
            if isinstance(d, dict):
                d["elapsed_ms"] = round((time.perf_counter() - _t0) * 1000, 1)
                return json.dumps(d, ensure_ascii=False, indent=2)
        except Exception:  # 尽力而为
            pass
        return r
    return _wrapped


@_timed_json
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
    # IDE 增强 172（安全）：root/file 过 _check_path 沙盒校验（防越界
    # 符号图索引/影响面分析读取沙盒外文件）；转 str 防 WindowsPath
    # 序列化失败（探针 201 抓出：impact 的 affected 含 WindowsPath）
    root = str(_check_path(root))
    if file:
        file = str(_check_path(file))
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
                               "index": stats,
                               # IDE 增强 208：调用链建议（改动前看谁在调）
                               "advice": (f"{len(hits)} 处调用 {symbol}——"
                                          f"改动签名前先过调用方"
                                          if hits else f"无调用者——{symbol} 是孤立符号")},
                              ensure_ascii=False, indent=2)
        if query == "callees":
            if not symbol:
                raise ValueError("callees 需要 symbol")
            hits = gi.callees_of(_resolve_symbol(gi, root, symbol))
            return json.dumps({"ok": True, "query": "callees", "symbol": symbol,
                               "count": len(hits), "callees": hits,
                               "index": stats,
                               # IDE 增强 208：callees 建议（它调了什么）
                               "advice": (f"{symbol} 调用 {len(hits)} 个符号——"
                                          f"改依赖前确认下游不破"
                                          if hits else f"{symbol} 不调用其他符号")},
                              ensure_ascii=False, indent=2)
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
                               "index": stats,
                               # IDE 增强 201：影响面建议（改动前评估范围）
                               "advice": (f"影响 {len(hits)} 个文件（深度 {depth}）——"
                                          f"改动前先过一遍，防连带破坏"
                                          if hits else "无影响文件——改动安全")},
                              ensure_ascii=False, indent=2)
        if query == "hubs":
            hits = gi.hubs(top=top)
            return json.dumps({"ok": True, "query": "hubs", "count": len(hits),
                               "hubs": hits, "index": stats,
                               # IDE 增强 193：核心符号建议（改动前先看调用方）
                               "advice": (f"核心符号 {len(hits)} 个——"
                                          f"首个 {hits[0].get('name')}（引用 "
                                          f"{hits[0].get('refs', '?')} 次），"
                                          f"改动前先查调用方（query=callers）"
                                          if hits else "无核心符号")},
                              ensure_ascii=False, indent=2)
        if query == "communities":
            hits = gi.communities(max_communities=top)
            return json.dumps({"ok": True, "query": "communities",
                               "count": len(hits), "communities": hits,
                               "index": stats,
                               # IDE 增强 218：社区建议（模块聚簇提示——
                               # 社区内耦合高，跨社区改动先评估）
                               "advice": (f"发现 {len(hits)} 个模块社区——"
                                          f"社区内耦合高，改动先看同社区依赖"
                                          if hits else "无社区结构——模块间弱耦合")},
                              ensure_ascii=False, indent=2)
        # search
        hits = gi.search_symbols(name or symbol, limit=top)
        return json.dumps({"ok": True, "query": "search", "name": name or symbol,
                           "count": len(hits), "symbols": hits,
                           "index": stats,
                           # IDE 增强 212：命中建议（符号搜索引导）
                           "advice": (f"找到 {len(hits)} 个符号——"
                                      f"首个 {hits[0].get('name')}（"
                                      f"{hits[0].get('file', '')}），"
                                      f"查看调用链用 query=callers"
                                      if hits else "无匹配符号——换关键词或模糊名")},
                          ensure_ascii=False, indent=2)
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
    # IDE 增强 183（安全）：root/out 过 _check_path——out 任意路径会写出
    # 沙盒外（写文件越界）
    root = _check_path(root)
    if out:
        out = str(_check_path(out))
    if not root or not os.path.isdir(root):
        raise ValueError(f"root 必须存在: {root}")
    try:
        from repo_wiki import generate_wiki
    except ImportError:
        _dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, _dir)
        from repo_wiki import generate_wiki  # noqa: F811
    if not out:
        out = os.path.join(str(root), ".unified-rx-index", "WIKI.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    return json.dumps(generate_wiki(str(root), out), ensure_ascii=False, indent=2)


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
    _t0 = time.perf_counter()
    # IDE 增强 176：rules 透传（三路都过滤——对称 std_check 173/bug_scan 174/cb_scan 175）
    _only = str(args.get("rules", ""))
    results = {"path": path, "bug_scan": [], "std_check": [], "ui_check": [], "errors": []}

    def run_one(tool_fn: str, name: str) -> None:
        try:
            fn = {"bug_scan": _tool_bug_scan, "std_check": _tool_std_check,
                  "ui_check": _tool_ui_check}[tool_fn]
            r = fn({"path": path, "max_files": max_files, "rules": _only})
            text = r[0].text if isinstance(r, list) else str(r)
            results[name] = json.loads(text) if text.startswith("{") else {"raw": text[:200]}
        except Exception as e:  # noqa: BLE001
            results["errors"].append(f"{name}: {e}")

    with _CONCURRENCY_SEM:
        pool = _pool()
        futures = [pool.submit(run_one, t, n) for t, n in
                   (("bug_scan", "bug_scan"), ("std_check", "std_check"), ("ui_check", "ui_check"))]
        for fut in as_completed(futures):
            fut.result()  # 异常已在 run_one 内捕获
    # IDE 增强 135：最严重问题提示（与 project_scan 对称）
    _worst = ""
    _bs = results.get("bug_scan")
    if isinstance(_bs, dict):
        _errs = [i for i in _bs.get("issues", [])
                 if str(i.get("severity", "")).lower() in ("error", "critical")]
        if _errs:
            _e = _errs[0]
            _worst = (f"最优先：{os.path.basename(str(_e.get('file', '')))}:{_e.get('line')} "
                      f"[{_e.get('rule')}] error（共 {len(_errs)} 条）")
    results["advice"] = _worst or "无 error——按 warning/占位/UI 分布排查（看 detail）"
    # IDE 增强 189：顶层规则汇总（union 三路子结果 available_rules——
    # 聚合入口同样可发现 rules= 可传哪些）
    _ar: set = set()
    for _k in ("bug_scan", "std_check", "ui_check"):
        _sub = results.get(_k)
        if isinstance(_sub, dict):
            _ar |= set(_sub.get("available_rules", []) or [])
    results["available_rules"] = sorted(_ar)
    # IDE 增强 283：聚合入口规则引擎标注（静态映射——不依赖子结果缓存，
    # 旧缓存无新字段也不缺失；AI 在 vuln_scan 顶层看到引擎来源）
    results["rule_engines"] = {
        "ui_root_missing": "bevy", "camera_missing": "bevy",
        "mode_isolation": "bevy", "focus_pass": "bevy",
        "font_missing": "bevy", "z_ordering": "bevy",
        "no_interaction": "bevy/godot/unity/flutter",
    }
    # IDE 增强 241：聚合耗时（ms——聚合入口收官）
    results["elapsed_ms"] = round((time.perf_counter() - _t0) * 1000, 1)
    # IDE 增强 285：聚合语言画像（union 各子结果 languages——取 max 防
    # 多路扫同批文件重复计数；vuln 顶层看到项目语言组成）
    _langs: dict[str, int] = {}
    for _k in ("bug_scan", "std_check", "ui_check"):
        _sub = results.get(_k)
        if isinstance(_sub, dict) and isinstance(_sub.get("languages"), dict):
            for _l, _c in _sub["languages"].items():
                _langs[_l] = max(_langs.get(_l, 0), int(_c))
    if _langs:
        results["languages"] = dict(sorted(_langs.items(), key=lambda kv: -kv[1]))
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
    _t0 = time.perf_counter()
    with_ui = bool(args.get("ui", True))  # Bevy 项目才有 .rs UI；非 Rust 项目可关
    # IDE 增强 177：rules 透传（四路都过滤——对称 vuln_scan 176）
    _only = str(args.get("rules", ""))
    results = {"path": path, "bug_scan": [], "std_check": [], "ui_check": [],
               "cb_scan": [], "errors": []}

    def run_one(tool_fn, name, extra=None):
        try:
            a = {"path": path, "max_files": max_files, "rules": _only}
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
    with _CONCURRENCY_SEM:
        pool = _pool()
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
    except Exception:  # 尽力而为
        pass
    # IDE 增强 134：最严重问题提示（error 级优先——AI 一眼看到从哪查起）
    _worst = ""
    _bs = results.get("bug_scan")
    if isinstance(_bs, dict):
        _errs = [i for i in _bs.get("issues", [])
                 if str(i.get("severity", "")).lower() in ("error", "critical")]
        if _errs:
            _e = _errs[0]
            _worst = (f"最优先：{os.path.basename(str(_e.get('file', '')))}:{_e.get('line')} "
                      f"[{_e.get('rule')}] error（共 {len(_errs)} 条 error）")
    results["advice"] = _worst or "无 error 级问题——按 warning/info 分布排查（看 detail）"
    # IDE 增强 190（里程碑）：顶层规则汇总（union 四路子结果——
    # 项目级入口同样可发现 rules= 可传哪些，对称 vuln_scan 189）
    _ar: set = set()
    for _k in ("bug_scan", "std_check", "ui_check", "cb_scan"):
        _sub = results.get(_k)
        if isinstance(_sub, dict):
            _ar |= set(_sub.get("available_rules", []) or [])
    results["available_rules"] = sorted(_ar)
    # IDE 增强 283：project_scan 顶层规则引擎标注（静态映射——对称 vuln）
    results["rule_engines"] = {
        "ui_root_missing": "bevy", "camera_missing": "bevy",
        "mode_isolation": "bevy", "focus_pass": "bevy",
        "font_missing": "bevy", "z_ordering": "bevy",
        "no_interaction": "bevy/godot/unity/flutter",
    }
    # IDE 增强 242：聚合耗时（ms——聚合入口收官）
    results["elapsed_ms"] = round((time.perf_counter() - _t0) * 1000, 1)
    # IDE 增强 285：project 聚合语言画像（只用 bug_scan——cb_scan 扫同批
    # 文件会双计；语言画像以 bug_scan 全量扫描为准）
    _sub = results.get("bug_scan")
    if isinstance(_sub, dict) and isinstance(_sub.get("languages"), dict):
        results["languages"] = dict(
            sorted(_sub["languages"].items(), key=lambda kv: -kv[1]))
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
    _t0 = time.perf_counter()
    max_files = int(args.get("max_files", 100))
    ui = bool(args.get("ui", True))
    # IDE 增强 178：rules 透传（每项目 project_scan 都过滤——多项目入口收官）
    _only = str(args.get("rules", ""))
    results = {"roots": roots, "projects": [], "errors": []}

    def scan_project(root: str) -> None:
        if auto_roots and _scan_excluded(root):
            return  # 自动发现的默认 roots 过排除清单（不扫 Steam/无关目录）
        try:
            r = _call("project_scan", {"path": root, "max_files": max_files,
                                       "ui": ui, "rules": _only})
            text = r[0].text if isinstance(r, list) else str(r)
            results["projects"].append({
                "root": root,
                "result": json.loads(text) if text.startswith("{") else {"raw": text[:200]},
            })
        except Exception as e:  # noqa: BLE001
            results["errors"].append(f"{root}: {e}")

    with _CONCURRENCY_SEM:
        pool = _pool()
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
    except Exception:  # 尽力而为
        pass
    # IDE 增强 139：最严重项目提示（各项目 advice 汇总取 error 最多者）
    _worst_proj = ""
    _best_adv = ""
    for p in results.get("projects", []):
        _res = p.get("result", {})
        _det = _res.get("detail", {}) if isinstance(_res, dict) else {}
        _adv = _det.get("advice", "") if isinstance(_det, dict) else ""
        if _adv and "最优先" in _adv:
            _worst_proj = os.path.basename(str(p.get("root", "")).rstrip("/\\"))
            _best_adv = _adv
            break
    results["advice"] = (_worst_proj and f"最严重项目：{_worst_proj}——{_best_adv}") \
        or "各项目无 error 级问题"
    # IDE 增强 191：顶层规则汇总（union 各项目 available_rules——
    # 多项目入口同样可发现 rules= 可传哪些，对称 vuln 189/project 190）
    _ar: set = set()
    for p in results.get("projects", []):
        _res = p.get("result", {})
        _det = _res.get("detail", {}) if isinstance(_res, dict) else {}
        if isinstance(_det, dict):
            _ar |= set(_det.get("available_rules", []) or [])
    # IDE 增强 287：full_scan 多项目语言总览（各项目 languages 聚合——
    # 多项目矩阵语言组成一眼可见）
    _langs: dict[str, int] = {}
    _proj_langs: dict[str, dict] = {}
    for p in results.get("projects", []):
        _res = p.get("result", {})
        _det = _res.get("detail", {}) if isinstance(_res, dict) else {}
        if isinstance(_det, dict) and isinstance(_det.get("languages"), dict):
            _pl = _det["languages"]
            _proj_langs[os.path.basename(str(p.get("root", "")).rstrip("/\\"))] = _pl
            for _l, _c in _pl.items():
                _langs[_l] = max(_langs.get(_l, 0), int(_c))
    if _langs:
        results["languages"] = dict(sorted(_langs.items(), key=lambda kv: -kv[1]))
        results["project_languages"] = _proj_langs
    results["available_rules"] = sorted(_ar)
    # IDE 增强 243：聚合耗时（ms——多项目聚合收官）
    results["elapsed_ms"] = round((time.perf_counter() - _t0) * 1000, 1)
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
    _t0 = time.perf_counter()
    res = guard_text(text, root=root, tool_names=tool_names)
    res["ok"] = True
    res["tool_names_checked"] = len(tool_names)
    # IDE 增强 235：守卫耗时（ms——性能可见收官）
    res["elapsed_ms"] = round((time.perf_counter() - _t0) * 1000, 1)
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
# ── bug 扫描族（2026-08-15 拆出——R2/R3 拆上帝文件；工具行为零变化）──
from bug_scan_core import (  # noqa: F401  re-export 保持调用方不变
    _BUG_BUILTINS, _MAX_READ, _bug_is_open, _bug_const_zero, _bug_seq_len,
    _bug_issue, _bug_direct_defs, _bug_func_args, _bug_is_none_guarded,
    _ast_children, _bug_check_deref, _bug_check_seq, _bug_scope_scan,
    _bug_resource_leak, _bug_scan_file,
    _TRACEBACK_RE, _SIMPLE_POS_RE,  # bug_locate 共用
)


# IDE 增强 253/254：多语言扫描扩展（2026-08-14 用户点名"没有多语言处理 包括扫描"）
# ——go/ts/js/gd/c/cpp 轻量确定性文本规则（低误报；AST 精度留给 py/rs）
_BUG_SCAN_EXTS = {".py", ".rs", ".go", ".ts", ".tsx", ".js", ".jsx",
                  ".gd", ".c", ".cpp", ".h", ".hpp",
                  ".cs", ".lua", ".sh", ".bash",
                  ".java", ".kt", ".kts", ".swift", ".php", ".rb", ".ps1", ".dart",
                  # IDE 增强 464：别名扩展（.cc/.cxx→cpp、.hh/.hxx→hpp、.zsh→sh）
                  ".cc", ".cxx", ".hh", ".hxx", ".zsh"}
# IDE 增强 256：分析入口统一扩展（kb_query/semantic_search/explore_code/
# repo_wiki 全语言——用户点名"没有多语言处理"，三处白名单此前缺 go/c/cpp）
_ANALYZE_EXTS = {".rs", ".py", ".go", ".ts", ".tsx", ".js", ".jsx",
                 ".gd", ".c", ".h", ".cpp", ".hpp", ".cc",
                 ".cs", ".lua", ".sh", ".bash",
                 ".java", ".kt", ".kts", ".swift", ".php", ".rb", ".ps1", ".dart",
                 ".toml", ".md", ".ron", ".json", ".yaml", ".yml"}

# (正则, 规则名, 严重度, 消息) 每语言一组——只报确定性模式：
# 调试残留输出 / 裸 panic / goto 混乱 / any 滥用
_MULTI_LANG_RULES: dict[str, list[tuple]] = {
    ".go": [(r"\bfmt\.Println?\s*\(", "debug_residue", "warning",
             "调试残留（fmt.Print 生产输出——建议删或转日志）"),
            (r"\bpanic\s*\(", "panic", "warning",
             "裸 panic（生产崩溃——建议返回 error）"),
            (r"\bgo\s+[A-Za-z_][\w.]*\s*\(", "goroutine_sync", "info",
             "go 启动协程无同步（共享变量读写竞态风险——建议加锁/通道）"),
            (r"\brecover\s*\(\).*$", "recover_ignored", "warning",
             "recover() 返回值被忽略（panic 吞掉但无处理——建议记录日志）"),
            (r",\s*_\s*:?=\s*[A-Za-z_][\w.]*\s*\(", "err_ignored", "warning",
             "err 忽略（x, _ := f()——错误被丢弃——建议检查 err 并处理")],
    ".ts": [(r"console\.log\s*\(", "debug_residue", "warning",
             "调试残留（console.log——建议删或转日志）"),
            (r"\bany\s*[\):,=\s]", "any_abuse", "info",
             "any 类型滥用（TypeScript any——建议具体类型）"),
            (r"\beval\s*\(", "dynamic_exec", "error",
             "eval() 动态执行——输入不可信时任意代码注入，建议安全替代"),
            (r"(?<![=!<>])==(?!=)", "loose_eq", "warning",
             "== 宽松比较（类型强转坑——建议 ===/!==）"),
            (r"setTimeout\s*\(\s*['\"]", "string_exec", "warning",
             "setTimeout 字符串参数（eval 变体——建议传函数引用）")],
    ".tsx": [(r"console\.log\s*\(", "debug_residue", "warning",
              "调试残留（console.log——建议删或转日志）"),
             (r"\bany\s*[\):,=\s]", "any_abuse", "info",
              "any 类型滥用（TypeScript any——建议具体类型）"),
             (r"\beval\s*\(", "dynamic_exec", "error",
              "eval() 动态执行——输入不可信时任意代码注入，建议安全替代"),
             (r"(?<![=!<>])==(?!=)", "loose_eq", "warning",
              "== 宽松比较（类型强转坑——建议 ===/!==）"),
             (r"dangerouslySetInnerHTML", "xss_risk", "warning",
              "dangerouslySetInnerHTML（React 直接注入 HTML——XSS 风险，"
              "建议转义或 textContent）"),
             (r"setTimeout\s*\(\s*['\"]", "string_exec", "warning",
              "setTimeout 字符串参数（eval 变体——建议传函数引用）")],
    ".js": [(r"console\.log\s*\(", "debug_residue", "warning",
             "调试残留（console.log——建议删或转日志）"),
            (r"\beval\s*\(", "dynamic_exec", "error",
             "eval() 动态执行——输入不可信时任意代码注入，建议安全替代"),
            (r"\.innerHTML\s*=", "xss_risk", "warning",
             "innerHTML 赋值（XSS 注入风险——建议 textContent/createElement）"),
            (r"(?<![=!<>])==(?!=)", "loose_eq", "warning",
             "== 宽松比较（类型强转坑——建议 ===/!==）"),
            (r"setTimeout\s*\(\s*['\"]", "string_exec", "warning",
             "setTimeout 字符串参数（eval 变体——建议传函数引用）"),
            (r"\bvar\s+[A-Za-z_$][\w$]*", "var_leak", "info",
             "var 声明（函数级作用域泄漏——建议 let/const 块级）"),
            (r"\bparseInt\s*\(\s*[A-Za-z_$][\w$]*\s*\)", "parseint_radix", "warning",
             "parseInt 无基数（旧引擎 '08' 解析为 0——建议 parseInt(x, 10)")],
    ".jsx": [(r"console\.log\s*\(", "debug_residue", "warning",
              "调试残留（console.log——建议删或转日志）"),
             (r"\beval\s*\(", "dynamic_exec", "error",
              "eval() 动态执行——输入不可信时任意代码注入，建议安全替代"),
             (r"(?<![=!<>])==(?!=)", "loose_eq", "warning",
              "== 宽松比较（类型强转坑——建议 ===/!==）"),
             (r"dangerouslySetInnerHTML", "xss_risk", "warning",
              "dangerouslySetInnerHTML（React 直接注入 HTML——XSS 风险，"
              "建议转义或 textContent）"),
             (r"setTimeout\s*\(\s*['\"]", "string_exec", "warning",
              "setTimeout 字符串参数（eval 变体——建议传函数引用）")],
    ".gd": [(r"\bprint\s*\(", "debug_residue", "warning",
             "调试残留（print——建议删或转 push_warning 日志）"),
            (r"\bget_node\s*\([^)]*\)\s*\.", "null_access",
             "warning",
             "get_node 直接访问（节点不存在返回 null——建议 get_node_or_null 判空）"),
            (r"\bfree(\s*[^)]*)\s*;?", "free_unsafe", "warning",
             "free() 裸用（信号回调中崩溃风险——建议 queue_free 安全释放）")],
    ".c": [(r"\bprintf\s*\(", "debug_residue", "warning",
            "调试残留（printf——建议删或转日志）"),
           (r"\bgoto\s+[A-Za-z_]\w*\s*;", "goto_used", "warning",
            "goto 使用（控制流混乱——建议结构化替代）"),
           (r"\bstrcpy\s*\(|\bstrcat\s*\(|\bsprintf\s*\(", "unsafe_string",
            "warning",
            "不安全字符串函数（strcpy/strcat/sprintf——缓冲区溢出，建议 strncpy/snprintf）"),
           (r"\bgets\s*\(", "gets_unsafe", "error",
            "gets() 无法安全使用（缓冲区溢出——建议 fgets）"),
           (r"\b\w*\s*=\s*realloc\s*\(", "realloc_unchecked", "warning",
            "realloc 直接赋值（失败返回 NULL 丢原指针——建议临时变量+判空）")],
    ".cpp": [(r"std::cout\s*<<", "debug_residue", "warning",
              "调试残留（std::cout——建议删或转日志）"),
             (r"\bgoto\s+[A-Za-z_]\w*\s*;", "goto_used", "warning",
              "goto 使用（控制流混乱——建议结构化替代）"),
             (r"\bstrcpy\s*\(|\bstrcat\s*\(|\bsprintf\s*\(", "unsafe_string",
              "warning",
              "不安全字符串函数（strcpy/strcat/sprintf——缓冲区溢出，建议 strncpy/snprintf）"),
             (r"\bgets\s*\(", "gets_unsafe", "error",
              "gets() 无法安全使用（缓冲区溢出——建议 fgets）"),
             (r"\b\w*\s*=\s*realloc\s*\(", "realloc_unchecked", "warning",
              "realloc 直接赋值（失败返回 NULL 丢原指针——建议临时变量+判空）")],
    ".h": [  # IDE 增强 471：头文件复用 c 规则（strcpy/gets 在头文件同样危险）
(r"\bprintf\s*\(", "debug_residue", "warning",
            "调试残留（printf——建议删或转日志）"),
           (r"\bgoto\s+[A-Za-z_]\w*\s*;", "goto_used", "warning",
            "goto 使用（控制流混乱——建议结构化替代）"),
           (r"\bstrcpy\s*\(|\bstrcat\s*\(|\bsprintf\s*\(", "unsafe_string",
            "warning",
            "不安全字符串函数（strcpy/strcat/sprintf——缓冲区溢出，建议 strncpy/snprintf）"),
           (r"\bgets\s*\(", "gets_unsafe", "error",
            "gets() 无法安全使用（缓冲区溢出——建议 fgets）"),
           (r"\b\w*\s*=\s*realloc\s*\(", "realloc_unchecked", "warning",
            "realloc 直接赋值（失败返回 NULL 丢原指针——建议临时变量+判空）")],
    ".hpp": [  # IDE 增强 471：头文件复用 cpp 规则（std::cout/strcpy 同样危险）
(r"std::cout\s*<<", "debug_residue", "warning",
              "调试残留（std::cout——建议删或转日志）"),
             (r"\bgoto\s+[A-Za-z_]\w*\s*;", "goto_used", "warning",
              "goto 使用（控制流混乱——建议结构化替代）"),
             (r"\bstrcpy\s*\(|\bstrcat\s*\(|\bsprintf\s*\(", "unsafe_string",
              "warning",
              "不安全字符串函数（strcpy/strcat/sprintf——缓冲区溢出，建议 strncpy/snprintf）"),
             (r"\bgets\s*\(", "gets_unsafe", "error",
              "gets() 无法安全使用（缓冲区溢出——建议 fgets）"),
             (r"\b\w*\s*=\s*realloc\s*\(", "realloc_unchecked", "warning",
              "realloc 直接赋值（失败返回 NULL 丢原指针——建议临时变量+判空）")],
    # IDE 增强 265：C#（Unity）/ Lua / Bash（游戏与脚本语言——用户点名）
    ".cs": [(r"\bConsole\.Write(?:Line)?\s*\(", "debug_residue", "warning",
             "调试残留（Console.Write——建议删或转日志）"),
            (r"\bDebug\.Log\s*\(", "debug_residue", "warning",
             "调试残留（Debug.Log——建议删或转日志）"),
            (r"\bthrow\s+new\s+Exception\s*\(", "bare_throw", "warning",
             "裸 throw new Exception（应抛具体异常类型——建议 ArgumentException 等）"),
            (r"\(\s*(?:string|int|long|double|float|bool|object)\s*\)\s*[A-Za-z_]", "unsafe_cast", "warning",
             "(T)x 强制转换（类型不匹配时抛 InvalidCastException——建议 as 安全转换或 is 检查）"),
            (r"\basync\s+void\s+[A-Za-z_]\w*\s*\(", "async_void", "warning",
             "async void（异常不可捕获——崩溃直接冒泡到线程；"
             "建议 async Task，仅事件处理器除外）")],
    ".lua": [(r"\bprint\s*\(", "debug_residue", "warning",
              "调试残留（print——建议删或转日志）"),
             (r"\bos\.execute\s*\(", "shell_injection", "error",
              "os.execute 命令注入（输入不可信时任意命令执行——建议参数化）"),
             (r"\bloadstring\s*\(", "dynamic_exec", "error",
              "loadstring 动态执行（eval 等价——输入不可信时任意代码执行）")],
    ".sh": [(r"\beval\s+[\"']", "shell_injection", "error",
             "eval 命令注入（输入不可信时任意命令执行——建议参数化/白名单）"),
            (r"\brm\s+-rf\s+/\s*(?:\*|\s|$)", "destructive", "error",
             "rm -rf /（毁灭性命令——建议精确路径+确认）"),
            (r"\bcurl\s+[^\|]*\|\s*(?:sudo\s+)?(?:bash|sh)\b", "pipe_exec",
             "warning",
             "curl | bash（从网络管道执行——供应链风险，建议先下载审查）")],
    # IDE 增强 269：Java/Kotlin/Swift/PHP/Ruby/PowerShell（6 大主流语言）
    ".java": [(r"\bSystem\.out\.print(?:ln)?\s*\(", "debug_residue", "warning",
               "调试残留（System.out.print——建议删或转日志框架）"),
              (r"\.printStackTrace\s*\(", "debug_residue", "warning",
               "printStackTrace（堆栈打印到 stderr——建议日志框架记录）"),
              (r"\bClass\.forName\s*\(", "dynamic_load", "warning",
               "Class.forName 动态加载（输入不可信时任意类加载——建议白名单）"),
              (r"[A-Za-z_]\w*\s*==\s*[\"']", "string_equals", "warning",
               "== 字符串比较（Java 比较引用非内容——建议 .equals()）"),
              (r"\b[A-Za-z_]\w*\s*\.equals\s*\(", "equals_npe", "warning",
               "s.equals(...) 调用（s 为 null 时 NPE——建议常量/字面量前置 "
               "\"x\".equals(s) 或 Objects.equals）")],
    ".kt": [(r"!!(?=\s|\)|,|;|$)", "nonnull_assert", "warning",
             "!! 非空断言（Kotlin 中 null 时抛 NPE——建议安全调用 ?. 或判空）"),
            (r"\bas\s+[A-Za-z_]\w*(?:\?)?\s*(?:[;)]|$)", "unsafe_cast", "warning",
             "as 强转（类型不匹配时抛 ClassCastException——建议 as? 安全转换或 is 检查）"),
            (r"\bprintln\s*\(", "debug_residue", "warning",
             "调试残留（println——建议删或转日志）")],
    ".kts": [(r"!!(?=\s|\)|,|;|$)", "nonnull_assert", "warning",
              "!! 非空断言（null 时抛 NPE——建议安全调用）"),
             (r"\bprintln\s*\(", "debug_residue", "warning",
              "调试残留（println——建议删或转日志）")],
    ".swift": [(r"\bprint\s*\(", "debug_residue", "warning",
                "调试残留（print——建议删或转 os_log）"),
               (r"!\s*(?:\.|\)|\)\s*\.|\s*=|\s*\()", "force_unwrap", "warning",
                "强制解包 !（nil 时崩溃——建议 guard let/if let 安全解包）"),
               (r"\bas!\s", "force_cast", "warning",
                "as! 强制转换（类型不匹配时崩溃——建议 as? 安全转换+判空）"),
               (r"\btry!\s", "force_try", "warning",
                "try! 强制错误处理（抛错即崩溃——建议 do-catch 或 try? 安全处理）")],
    ".php": [(r"\becho\s+|\bprint_r\s*\(", "debug_residue", "warning",
              "调试残留（echo/print_r——建议删或转日志）"),
             (r"\beval\s*\(", "dynamic_exec", "error",
              "eval() 动态执行——输入不可信时任意代码注入，建议安全替代"),
             (r"\bmysql_query\s*\(", "unsafe_sql", "warning",
              "mysql_query 直接拼接（SQL 注入风险——建议 PDO 预处理）"),
             (r"(?<![=!<>])==(?!=)", "loose_eq", "warning",
              "== 宽松比较（PHP 类型强转坑——建议 === 严格比较）"),
             (r"\$_(?:GET|POST|REQUEST|COOKIE)\s*\[", "unsanitized_input", "warning",
              "超全局输入直接使用（$_GET/$_POST 未过滤——XSS/SQL 注入风险，"
              "建议 filter_input/htmlspecialchars/预处理）")],
    ".rb": [(r"\bputs\s+|\bp\s+", "debug_residue", "warning",
             "调试残留（puts/p——建议删或转日志）"),
            (r"\beval\s*\(", "dynamic_exec", "error",
             "eval() 动态执行——输入不可信时任意代码注入，建议安全替代"),
            (r"\bsystem\s*\(", "shell_injection", "warning",
             "system() 命令执行（输入不可信时命令注入——建议参数化）")],
    ".ps1": [(r"\bWrite-Host\s+", "debug_residue", "warning",
              "调试残留（Write-Host——建议删或转 Write-Verbose 日志）"),
             (r"\bInvoke-Expression(?:\s*\(|\s+[$\w])", "shell_injection", "error",
              "Invoke-Expression（PowerShell eval——输入不可信时任意命令执行）"),
             (r"\bRemove-Item\s+[^\n]*\s-Recurse", "destructive", "warning",
              "Remove-Item -Recurse（递归删除——建议精确路径+确认）")],
    # IDE 增强 273：Dart（Flutter——移动/桌面 UI 主流语言）
    ".dart": [(r"[A-Za-z_][\w.]*!(?=\s*[.;,)\]])", "nonnull_assert", "warning",
               "! 非空断言（null 时抛异常——建议 ?. 安全调用或判空）"),
              (r"\bprint\s*\(", "debug_residue", "warning",
               "调试残留（print——建议删或转 debugPrint/日志）"),
              (r"\bdynamic\s+[A-Za-z_]\w*", "dynamic_abuse", "info",
               "dynamic 类型滥用（Dart dynamic——建议具体类型）"),
              (r"\bas\s+[A-Za-z_]\w*\s*[);]", "unsafe_cast", "warning",
               "as 强转（类型不匹配时抛异常——建议 is 检查后安全转换）")],
}


def _scan_c_null_deref(src: str, path: str) -> list:
    """c/cpp 空指针解引用（IDE 增强 260：NULL/0 初始化后解引用——
    确定性状态跟踪：`int *p = NULL;` → `*p`/`p->` 即 bug）。"""
    issues = []
    null_ptrs: dict[str, int] = {}
    lines = src.splitlines()
    for i, line in enumerate(lines, 1):
        m = re.search(r"\b([A-Za-z_]\w*)\s*=\s*(?:NULL|0)\s*;", line)
        if m:
            # 指针声明（名字含 p/ptr 或 * 前缀；排除整型变量 `int x = 0;`）
            decl = line[:m.start()]
            if "*" in decl or "ptr" in m.group(1).lower():
                null_ptrs[m.group(1)] = i
    for i, line in enumerate(lines, 1):
        for name, decl_line in null_ptrs.items():
            if i == decl_line:
                continue  # 声明行自身（`int *p = 0;`）不是解引用
            if re.search(rf"(?<!\w)\*{name}\b|\b{name}\s*->", line):
                col = line.find(name) + 1
                issues.append({
                    "file": str(Path(path).resolve()), "line": i,
                    "col": col,
                    "rule": "null_deref", "severity": "error",
                    "msg": f"空指针解引用（{name} 初始化为 NULL/0 于行 {decl_line}）",
                    "snippet": line.strip()[:80]})
                break
    return issues


def _scan_go_nil_map(src: str, path: str) -> list:
    """go nil map 写入（IDE 增强 261：`var m map[string]int` 后 `m["k"] = v`
    即 panic——确定性状态跟踪；make 初始化后不报）。"""
    issues = []
    nil_maps: dict[str, int] = {}
    lines = src.splitlines()
    for i, line in enumerate(lines, 1):
        m = re.search(r"\bvar\s+([A-Za-z_]\w*)\s+map\[", line)
        if m and not re.search(r"\bmake\s*\(", line):
            nil_maps[m.group(1)] = i
        # `m = make(...)` 之后解除
        m2 = re.search(r"\b([A-Za-z_]\w*)\s*=\s*make\s*\(", line)
        if m2:
            nil_maps.pop(m2.group(1), None)
    for i, line in enumerate(lines, 1):
        for name, decl_line in nil_maps.items():
            if re.search(rf"\b{name}\s*\[[^\]]*\]\s*=", line):
                issues.append({
                    "file": str(Path(path).resolve()), "line": i,
                    "col": line.find(name) + 1,
                    "rule": "nil_map_write", "severity": "error",
                    "msg": f"nil map 写入（{name} 声明于行 {decl_line} 未 make——写入即 panic，"
                           f"建议先 make/初始化）",
                    "snippet": line.strip()[:80]})
                break
    return issues


_CPP_ALIASES = {".cc": ".cpp", ".cxx": ".cpp", ".hh": ".hpp", ".hxx": ".hpp",
                ".bash": ".sh", ".zsh": ".sh"}


def _multi_lang_scan(path: str, src: str) -> list:
    """多语言轻量确定性扫描（go/ts/js/gd/c/cpp——文本规则）。

    AST 精度留给 Python（_bug_scan_file）与 Rust（rust_scan）；本函数
    服务其余语言。每行最多报一条（防多规则重复）。"""
    issues = []
    ext = os.path.splitext(path)[1].lower()
    # IDE 增强 464：.cc/.cxx 与 .cpp 同语言、.hh 与 .hpp 同语言（契约 p35 抓出：
    # .cc 此前落入 syntax_error——规则表未映射）
    ext = _CPP_ALIASES.get(ext, ext)
    rules = _MULTI_LANG_RULES.get(ext, [])
    if not rules:
        return issues
    for i, line in enumerate(src.splitlines(), 1):
        for pat, rule, sev, msg in rules:
            m = re.search(pat, line)
            if m:
                issues.append({
                    "file": str(Path(path).resolve()), "line": i,
                    "col": m.start() + 1, "rule": rule, "severity": sev,
                    "msg": msg, "snippet": line.strip()[:80]})
                break
    if ext in (".c", ".cpp"):
        # IDE 增强 260：c/cpp 空指针解引用（状态跟踪——行级规则无法表达）
        issues.extend(_scan_c_null_deref(src, path))
    elif ext == ".go":
        # IDE 增强 261：go nil map 写入（状态跟踪）
        issues.extend(_scan_go_nil_map(src, path))
    return issues


def _scan_file_dispatch(f: str) -> tuple[list, int]:
    """按后缀分发扫描：.py 用 _bug_scan_file（Python AST），.rs 用 rust_scan（tree-sitter），
    其余支持语言（go/ts/js/gd/c/cpp）用 _multi_lang_scan（轻量文本规则）。"""
    ext = os.path.splitext(f)[1].lower()
    # IDE 增强 464：.cc/.cxx/.hh/.hxx 别名归一（契约 p35 抓出）
    ext = _CPP_ALIASES.get(ext, ext)
    if ext == ".rs":
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
    if ext in _MULTI_LANG_RULES:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                src = fh.read()
        except OSError:
            return [], 0
        issues = _multi_lang_scan(f, src)
        # 挖漏洞增强：模板规则 DSL 对多语言文件也生效（Nuclei 概念——
        # js 原型污染等模板规则命中非 Python 文件）
        from bug_scan_core import ext_rules_scan
        ext_rules_scan(src, str(f), src.splitlines(), issues)
        return issues, len(src.splitlines())
    return _bug_scan_file(f)


def _tool_bug_scan(args: dict) -> "list[types.TextContent]":
    """静态扫描 bug 模式：未定义变量/None 解引用/资源泄漏/除零/越界。

    多文件目录扫描用 ProcessPoolExecutor 并行（CPU 密集 AST，子进程不吃 GIL）；
    进程池不可用（受限环境）时串行 fallback。结果与串行完全一致。
    单文件走 scan_cache（mtime/size 未变直接返回缓存，省重复扫描）。
    """
    p = _check_path(str(args["path"]))
    _t0 = time.perf_counter()
    max_files = int(args.get("max_files", _BUG_MAX_FILES))
    if not 1 <= max_files <= 500:
        raise ValueError("max_files 须在 1..500")
    # IDE 增强 174：规则过滤参数（在缓存命中前解析——缓存 key 含 rules，
    # 防过滤被缓存绕过）
    _only = {s.strip() for s in str(args.get("rules", "")).split(",") if s.strip()}
    # 单文件缓存（幂等只读；文件变了缓存失效）
    if p.is_file():
        try:
            import scan_cache
            hit = scan_cache.get("bug_scan", f"{p}|{sorted(_only)}")
            if hit is not None:
                return [_TC(json.dumps(hit, ensure_ascii=False))]
        except ImportError:  # 尽力而为
            pass
    files = []
    if p.is_file():
        if p.suffix.lower() in _BUG_SCAN_EXTS:
            files = [p]
        else:
            raise ValueError(f"仅支持语言文件（py/rs/go/ts/js/gd/c/cpp）: {p}")
    elif p.is_dir():
        for root, _, names in os.walk(p):
            for name in sorted(names):
                if name.lower().endswith(tuple(_BUG_SCAN_EXTS)):
                    files.append(Path(root) / name)
                    if len(files) >= max_files:
                        break
            if len(files) >= max_files:
                break
    else:
        raise ValueError(f"路径不存在: {p}")

    issues: list = []
    total_lines = 0
    # IDE 增强 284：项目语言画像（扫描文件后缀分布——AI 一眼知道
    # 项目有哪些语言，多语言仓库不再靠猜）
    lang_counts: dict[str, int] = {}
    for _f in files:
        _sfx = os.path.splitext(str(_f))[1].lower().lstrip(".")
        if _sfx:
            lang_counts[_sfx] = lang_counts.get(_sfx, 0) + 1
    languages = dict(sorted(lang_counts.items(), key=lambda kv: -kv[1]))
    # 单文件直接扫；多文件并行（≥8 文件起进程池——AST 为 CPU 密集；
    # 共享全局池：并发调用复用同一池，不再每次新建 8×N 子进程）
    if len(files) >= 8:
        try:
            ex = _proc_pool()
            for fi, ln in ex.map(_scan_file_dispatch, [str(f) for f in files]):
                issues.extend(fi)
                total_lines += ln
        except Exception:
            # 进程池不可用（受限环境/spawn 失败）→ 串行 fallback
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
    # IDE 增强 174：规则过滤（rules 逗号分隔——只报指定规则，对称 std_check 173）
    _only = {s.strip() for s in str(args.get("rules", "")).split(",") if s.strip()}
    if _only:
        issues = [i for i in issues if i.get("rule") in _only]
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
        # IDE 增强 284：项目语言画像（多语言仓库语言分布）
        "languages": languages,
        "noise_ratio": round(sev_counts["info"] / total, 3) if total else 0.0,
        "note": ("noise_ratio=info 占比（高即多为风格提示）；error 为确定性缺陷，"
                 "warn 为需审查项——参考 SCAN_QUALITY_ISSUES.md"),
        "issues": issues,
        # IDE 增强 227：扫描耗时（ms——对称 std 226，双入口收官）
        "elapsed_ms": round((time.perf_counter() - _t0) * 1000, 1),
    }
    # IDE 增强 148：最严重 bug 提示（error 优先——确定性缺陷先修）
    _errs = [i for i in issues
             if str(i.get("severity", "")).lower() in ("error", "critical")]
    # IDE 增强 205：问题最多文件（worst_file——扫描三入口收官，
    # 对称 std_check 204/ui_check 203）
    _worst_f, _worst_n = "", 0
    if issues:
        _fc: dict[str, int] = {}
        for i in issues:
            _k = str(i.get("file", ""))
            _fc[_k] = _fc.get(_k, 0) + 1
        _worst_f, _worst_n = max(_fc.items(), key=lambda kv: kv[1])
    result["worst_file"] = (f"{os.path.basename(_worst_f)}（{_worst_n} 条问题）"
                            if issues else "")
    # IDE 增强 210（里程碑）：最多问题规则（top_rule——对称 std_check 209，
    # 批量修复入口双入口）
    _rc: dict[str, int] = {}
    for i in issues:
        _r = str(i.get("rule", "unknown"))
        _rc[_r] = _rc.get(_r, 0) + 1
    if _rc:
        _tr, _tn = max(_rc.items(), key=lambda kv: kv[1])
        result["top_rule"] = f"{_tr}（{_tn} 条）"
    else:
        result["top_rule"] = ""
    # IDE 增强 221：文件级规则分布（file_rules——对称 std 219/ui 220，
    # 扫描三入口收官）
    _fr: dict[str, dict[str, int]] = {}
    for i in issues:
        _f = os.path.basename(str(i.get("file", "")))
        _r = str(i.get("rule", "unknown"))
        _fr.setdefault(_f, {})
        _fr[_f][_r] = _fr[_f].get(_r, 0) + 1
    result["file_rules"] = dict(sorted(_fr.items())[:20])
    if _errs:
        _e = _errs[0]
        result["advice"] = (f"最优先：{os.path.basename(str(_e.get('file', '')))}:"
                            f"{_e.get('line')} [{_e.get('rule')}] "
                            f"{str(_e.get('msg', ''))[:40]}（共 {len(_errs)} 条 error）")
    elif issues:
        result["advice"] = "无 error——按 warn 分布排查（多数为需审查项）"
    else:
        result["advice"] = "无问题"
    # IDE 增强 186：可用规则列表（rules= 过滤参数可传哪些——对称 std_check 185）
    # IDE 增强 280：available_rules 补全 23 语言规则（原静态 12 条 Python 规则——
    # 多语言规则缺失，AI 看不到可用规则清单）
    _ml_rules = {r for _rules in _MULTI_LANG_RULES.values()
                 for _, r, _, _ in _rules}
    _ml_rules |= {"null_deref", "nil_map_write"}
    # IDE 增强 281：rust_scan 规则（panic/unreachable/todo/unimplemented/
    # expect/unsafe/transmute/as/io——rust 规则名进清单）
    _ml_rules |= {"panic", "unreachable", "todo", "unimplemented", "expect",
                  "unsafe", "transmute", "as", "io"}
    result["available_rules"] = sorted({
        str(i.get("rule")) for i in issues
    } | {"divide_by_zero", "undefined_name", "none_deref", "unwrap",
         "resource_leak", "swallowed_exception", "index_out_of_range",
         "dynamic_exec", "shell_injection", "unsafe_pickle", "unsafe_yaml",
         "tar_extractall"} | _ml_rules)
    # 单文件成功结果入缓存（幂等只读；mtime/size 变了自动失效）
    if p.is_file():
        try:
            import scan_cache
            scan_cache.put("bug_scan", f"{p}|{sorted(_only)}", result)
        except ImportError:  # 尽力而为
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


def _tool_game_check(args: dict) -> "list[types.TextContent]":
    """游戏工程引擎中立检查（skill M1/M5：每帧 IO/输入节流/物理数量级）。"""
    from game_check import check_project
    p = _check_path(str(args.get("path", "")))
    rules = [r.strip() for r in str(args.get("rules", "")).split(",") if r.strip()]
    return [_TC(json.dumps(check_project(p, rules or None),
                           ensure_ascii=False, indent=2))]


def _tool_game_feel(args: dict) -> "list[types.TextContent]":
    """表现寄存器判定（skill M2：character/abstract/serious）。"""
    from game_check import judge_register, check_project
    p = _check_path(str(args.get("path", "")))
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                src = f.read()
        except OSError as e:
            return [_TC(json.dumps({"ok": False, "error": str(e)},
                                   ensure_ascii=False))]
        return [_TC(json.dumps(judge_register(src, p),
                               ensure_ascii=False, indent=2))]
    d = check_project(p)
    return [_TC(json.dumps({"ok": True, "registers": d["registers"],
                            "advice": d["advice"]},
                           ensure_ascii=False, indent=2))]


def _tool_game_api(args: dict) -> "list[types.TextContent]":
    """引擎 API 语义查询（防幻觉：未收录诚实拒绝）。"""
    from game_api import query_api
    engine = str(args.get("engine", ""))
    symbol = str(args.get("symbol", ""))
    return [_TC(json.dumps(query_api(engine, symbol),
                           ensure_ascii=False, indent=2))]


def _tool_game_verify(args: dict) -> "list[types.TextContent]":
    """可复现验证检查（skill M4：smoke/XDG/日志——无头验证不靠猜）。"""
    from game_check import verify_headless_setup
    p = _check_path(str(args.get("path", "")))
    return [_TC(json.dumps(verify_headless_setup(str(p)),
                           ensure_ascii=False, indent=2))]


def _tool_game_rules(args: dict) -> "list[types.TextContent]":
    """项目级游戏规则读写（通用默认 + 项目覆盖——在游戏文件里再搞一个）。"""
    from game_check import load_game_rules, save_game_rules
    p = _check_path(str(args.get("path", "")))
    # 核心合并：组合工具剥离外层 action 后，子动作经 sub_action 透传
    action = args.get("action") or args.get("sub_action", "load")
    if action == "save":
        rules = args.get("rules")
        if not isinstance(rules, dict):
            return [_TC(json.dumps({"ok": False, "error": "rules 须为 JSON 对象"},
                                   ensure_ascii=False))]
        return [_TC(json.dumps(save_game_rules(str(p), rules),
                               ensure_ascii=False, indent=2))]
    return [_TC(json.dumps(load_game_rules(str(p)),
                           ensure_ascii=False, indent=2))]


def _tool_watch_status(args: dict) -> "list[types.TextContent]":
    """实时监听状态（阶段1：文件改动监听线程）。"""
    from realtime_watch import watcher_status
    return [_TC(json.dumps(watcher_status(), ensure_ascii=False, indent=2))]


def _tool_predict_impact(args: dict) -> "list[types.TextContent]":
    """预知引擎（阶段2：改前预测影响面+教训+规则——改后跑 ide_fusion 确认）。"""
    from predict_impact import predict_impact
    root = _check_path(str(args.get("root", "")))
    symbol = str(args.get("symbol", ""))
    file_hint = str(args.get("file_hint", ""))
    return [_TC(json.dumps(predict_impact(str(root), symbol, file_hint),
                           ensure_ascii=False, indent=2))]


def _tool_speculate(args: dict) -> "list[types.TextContent]":
    """推测执行（阶段3：预测→预执行白名单只读→缓存秒回）。"""
    from speculate import speculate
    current_file = str(args.get("current_file", ""))
    recent_tools = args.get("recent_tools") or []
    recent_paths = args.get("recent_paths") or []
    return [_TC(json.dumps(speculate(current_file, recent_tools, recent_paths),
                           ensure_ascii=False, indent=2))]


def _tool_causal_trace(args: dict) -> "list[types.TextContent]":
    """因果建模（为什么错：失败→回溯因果链——git 提交+工具调用）。"""
    from causal_debug import causal_trace
    root = _check_path(str(args.get("root", "")))
    kw = str(args.get("fail_keyword", "fail"))
    return [_TC(json.dumps(causal_trace(str(root), kw),
                           ensure_ascii=False, indent=2))]


def _tool_bug_bisect(args: dict) -> "list[types.TextContent]":
    """git bisect 式二分定位（只读计划——execute=true 实际执行需 L4 授权）。"""
    from causal_debug import bug_bisect
    root = _check_path(str(args.get("root", "")))
    good = str(args.get("good_commit", ""))
    bad = str(args.get("bad_commit", "")) or "HEAD"
    cmd = str(args.get("test_cmd", "cargo test"))
    execute = bool(args.get("execute", False))
    return [_TC(json.dumps(bug_bisect(str(root), good, bad, cmd,
                                      execute=execute),
                           ensure_ascii=False, indent=2))]


def _tool_causal_link(args: dict) -> "list[types.TextContent]":
    """记录因果链（cause→effect 入 scan-log——行为链回放溯源）。"""
    from causal_debug import record_cause
    root = _check_path(str(args.get("root", "")))
    return [_TC(json.dumps(record_cause(str(root),
                                        str(args.get("effect", "")),
                                        str(args.get("cause", ""))),
                           ensure_ascii=False, indent=2))]


def _tool_optimize_code(args: dict) -> "list[types.TextContent]":
    """可微分编程落地（性能目标驱动优化器——规则驱动）。"""
    from differentiable_code import optimize_code
    p = _check_path(str(args.get("path", "")))
    goal = str(args.get("perf_goal", "响应时间<10ms"))
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            src = f.read()
    except OSError as e:
        return [_TC(json.dumps({"ok": False, "error": str(e)},
                               ensure_ascii=False))]
    return [_TC(json.dumps(optimize_code(src, str(p), goal),
                           ensure_ascii=False, indent=2))]


def _tool_code_embed(args: dict) -> "list[types.TextContent]":
    from differentiable_code import similar_functions, embed_function
    import ast as _ast
    p = _check_path(str(args.get("path", "")))
    compare = str(args.get("compare", ""))
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            src = f.read()
    except OSError as e:
        return [_TC(json.dumps({"ok": False, "error": str(e)},
                               ensure_ascii=False))]
    if compare:
        try:
            with open(_check_path(compare), encoding="utf-8",
                      errors="replace") as f:
                tgt = f.read()
        except OSError as e:
            return [_TC(json.dumps({"ok": False, "error": str(e)},
                                   ensure_ascii=False))]
        return [_TC(json.dumps(similar_functions(src, tgt),
                               ensure_ascii=False, indent=2))]
    # 单文件：函数嵌入清单
    try:
        tree = _ast.parse(src)
    except SyntaxError as e:
        return [_TC(json.dumps({"ok": False, "error": str(e)},
                               ensure_ascii=False))]
    fns = []
    for n in _ast.walk(tree):
        if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            fns.append(embed_function(n))
    return [_TC(json.dumps({"ok": True, "file": str(p), "functions": fns,
                            "count": len(fns)}, ensure_ascii=False, indent=2))]


def _tool_telemetry_status(args: dict) -> "list[types.TextContent]":
    """遥测状态快照（AI 可读，SGG PerfMeter 式）：存储状态 + 聚合
    （工具耗时 TOP/错误率/调用量）+ daemon 心跳表（卡死检测一眼看穿）。"""
    try:
        from telemetry_core import agg, status
    except ImportError:
        return [_TC(json.dumps({"ok": False,
                                "error": "telemetry_core 不可用"},
                               ensure_ascii=False))]
    since = args.get("since_ts")
    a = agg(since)
    s = status()
    if a is None or s is None:
        return [_TC(json.dumps({"ok": False, "error":
                                "rx-telemetry 不可用（RX_TELEMETRY=0 或 exe 缺失）"},
                               ensure_ascii=False))]
    tools = sorted(a.get("tools", {}).items(),
                   key=lambda kv: kv[1].get("max_ms", 0), reverse=True)
    slowest = [{"tool": n, **v} for n, v in tools[:10]]
    out = {
        "ok": True,
        "state": s,
        "summary": {k: a.get(k) for k in (
            "total_calls", "total_err", "overall_err_rate",
            "overall_avg_ms", "overall_p95_ms", "overall_max_ms")},
        "slowest_tools": slowest,
        "heartbeats": a.get("heartbeats", {}),
        "hint": "卡死检测：heartbeats 中某循环 last_ts 距今过久即异常；"
                "错误率骤升/某工具 max_ms 飙升即热点",
    }
    return [_TC(json.dumps(out, ensure_ascii=False, indent=1))]


def _tool_telemetry_snapshot(args: dict) -> "list[types.TextContent]":
    """SGG PerfMeter 式一键体检包：健康（卡死检测）+ 聚合 + 慢工具 +
    最近错误 + 告警 + 资源——AI 读一份就知系统全貌。"""
    try:
        from telemetry_core import (agg, health_check, read_alarms,
                                    recent_errors, status)
    except ImportError:
        return [_TC(json.dumps({"ok": False,
                                "error": "telemetry_core 不可用"},
                               ensure_ascii=False))]
    a = agg()
    s = status()
    if a is None or s is None:
        return [_TC(json.dumps({"ok": False, "error":
                                "rx-telemetry 不可用（RX_TELEMETRY=0 或 exe 缺失）"},
                               ensure_ascii=False))]
    health = health_check()
    tools = sorted(a.get("tools", {}).items(),
                   key=lambda kv: kv[1].get("max_ms", 0), reverse=True)
    slowest = [{"tool": n, **v} for n, v in tools[:10]]
    stale_loops = [n for n, h in health.get("loops", {}).items() if h.get("stale")]
    out = {
        "ok": True,
        "ts": time.time(),
        "state": s,
        "summary": {k: a.get(k) for k in (
            "total_calls", "total_err", "overall_err_rate",
            "overall_avg_ms", "overall_p95_ms", "overall_max_ms")},
        "health": health,
        "verdict": ("[WARN] 检测到卡死循环: " + ", ".join(stale_loops)
                    if stale_loops else "[OK] 全部 daemon 循环心跳正常"),
        "slowest_tools": slowest,
        "recent_errors": recent_errors(5),
        "alarms": read_alarms(10),
    }
    return [_TC(json.dumps(out, ensure_ascii=False, indent=1))]


def _tool_alarm_check(args: dict) -> "list[types.TextContent]":
    """告警规则引擎一轮：工具 P95 慢/错误率超限/daemon 卡死/总错误率
    → 新告警落盘 alarms.jsonl（30 分钟去重）——自动监控告警不靠人盯。"""
    try:
        from telemetry_core import check_alarms
    except ImportError:
        return [_TC(json.dumps({"ok": False,
                                "error": "telemetry_core 不可用"},
                               ensure_ascii=False))]
    th = args.get("thresholds")
    out = check_alarms(th if isinstance(th, dict) else None)
    out["ok"] = True
    return [_TC(json.dumps(out, ensure_ascii=False, indent=1))]


def _tool_failure_analyze(args: dict) -> "list[types.TextContent]":
    """根因分析（RCA）：traceback/失败文本 → 根因链报告（关联遥测/
    scan-log/git/告警，候选按证据强度排序）——定位问题不再靠猜。"""
    try:
        from failure_analyze import failure_analyze
    except ImportError:
        return [_TC(json.dumps({"ok": False,
                                "error": "failure_analyze 不可用"},
                               ensure_ascii=False))]
    text = str(args.get("text", ""))
    root = str(args.get("root", ""))
    limit = args.get("limit", 200)
    out = failure_analyze(text, root, limit)
    return [_TC(json.dumps(out, ensure_ascii=False, indent=1))]


def _tool_cov_scan(args: dict) -> "list[types.TextContent]":
    """覆盖率/死代码扫描：static（AST 未引用符号=隐形炸弹）或
    dynamic（coverage.py 实测，失败自动降级）。"""
    try:
        from cov_scan import cov_scan
    except ImportError:
        return [_TC(json.dumps({"ok": False,
                                "error": "cov_scan 不可用"},
                               ensure_ascii=False))]
    p = _check_path(str(args.get("path", "")))
    mode = str(args.get("mode", "static"))
    if mode not in ("static", "dynamic", "auto"):
        mode = "static"
    try:
        limit = min(max(int(args.get("limit", 2000)), 10), 10000)
    except (TypeError, ValueError):
        limit = 2000
    return [_TC(json.dumps(cov_scan(str(p), mode, limit),
                           ensure_ascii=False, indent=1))]


def _tool_stress_scan(args: dict) -> "list[types.TextContent]":
    """压力测试：扫描日志/遥测高并发 append（丢数据检测）+ 大仓库遍历/
    大文件读取计时——工具集自身不崩不卡才敢说"强"。"""
    try:
        from stress_scan import stress_scan
    except ImportError:
        return [_TC(json.dumps({"ok": False,
                                "error": "stress_scan 不可用"},
                               ensure_ascii=False))]
    path = str(args.get("path", ""))
    if path:
        path = str(_check_path(path))
    mode = str(args.get("mode", "auto"))
    if mode not in ("auto", "log", "telemetry", "index", "file"):
        mode = "auto"
    try:
        scale = min(max(int(args.get("scale", 100000)), 100), 1000000)
    except (TypeError, ValueError):
        scale = 100000
    try:
        timeout = min(max(int(args.get("timeout", 300)), 30), 600)
    except (TypeError, ValueError):
        timeout = 300
    return [_TC(json.dumps(stress_scan(path, mode, scale, timeout),
                           ensure_ascii=False, indent=1))]


def _tool_replay_record(args: dict) -> "list[types.TextContent]":
    """录制一步操作（工具调用/命令）到 replay 序列——崩溃复现的第一步。"""
    try:
        from replay_core import replay_record
    except ImportError:
        return [_TC(json.dumps({"ok": False,
                                "error": "replay_core 不可用"},
                               ensure_ascii=False))]
    name = str(args.get("name", ""))
    step = args.get("step")
    if not isinstance(step, dict):
        return [_TC(json.dumps({"ok": False,
                                "error": "step 必须是对象 {type, tool/tool, args…}"},
                               ensure_ascii=False))]
    return [_TC(json.dumps(replay_record(name, step),
                           ensure_ascii=False, indent=1))]


def _tool_replay_run(args: dict) -> "list[types.TextContent]":
    """重放录制的操作序列——偶现变必现，定位崩溃第一步。"""
    try:
        from replay_core import replay_run, replay_list
    except ImportError:
        return [_TC(json.dumps({"ok": False,
                                "error": "replay_core 不可用"},
                               ensure_ascii=False))]
    name = str(args.get("name", ""))
    if not name:
        return [_TC(json.dumps(replay_list(), ensure_ascii=False, indent=1))]
    stop = bool(args.get("stop_on_fail", True))
    return [_TC(json.dumps(replay_run(name, stop),
                           ensure_ascii=False, indent=1))]


def _tool_sage_scan(args: dict) -> "list[types.TextContent]":
    """SAGE 式语义回归优先级：commit 变更 + 语义标签 → 优先测试清单
    （复用 pr_oracle TestMapper，扩展不可用降级启发式）。"""
    try:
        from sage_scan import sage_scan
    except ImportError:
        return [_TC(json.dumps({"ok": False,
                                "error": "sage_scan 不可用"},
                               ensure_ascii=False))]
    root = str(_check_path(str(args.get("root", ""))))
    try:
        commits = min(max(int(args.get("commits", 1)), 1), 20)
    except (TypeError, ValueError):
        commits = 1
    since = str(args.get("since", ""))
    return [_TC(json.dumps(sage_scan(str(root), commits, since),
                           ensure_ascii=False, indent=1))]


def _tool_code_search(args: dict) -> "list[types.TextContent]":
    """本地语义代码检索（Rust rx-search，CocoIndex/codesearch 式）：自然语言/
    中文/符号查询 → 文件:行——零依赖快速版；explore_code 关键词失败自动兜底。"""
    try:
        from search_core import search
    except ImportError:
        return [_TC(json.dumps({"ok": False,
                                "error": "search_core 不可用"},
                               ensure_ascii=False))]
    q = str(args.get("query", "")).strip()
    if not q:
        return [_TC(json.dumps({"ok": False, "error": "query 必填"},
                               ensure_ascii=False))]
    root = str(args.get("root", "")).strip()
    if root:
        root = str(_check_path(root))
    try:
        k = min(max(int(args.get("k", 20)), 1), 50)
    except (TypeError, ValueError):
        k = 20
    out = search(q, root, k)
    if out is None:
        return [_TC(json.dumps({"ok": False,
                                "error": "rx-search 不可用（RX_SEARCH=0 或 exe 缺失）"},
                               ensure_ascii=False))]
    return [_TC(json.dumps(out, ensure_ascii=False, indent=1))]


def _tool_net_chaos(args: dict) -> "list[types.TextContent]":
    """弱网模拟（rx-net Clumsy 式本地 TCP 代理）：start/stop/status/sanity——
    延迟/丢包/乱序/限速注入——测试网络鲁棒性；subprocess 启停管理。"""
    try:
        import net_core
    except ImportError:
        return [_TC(json.dumps({"ok": False, "error": "net_core 不可用"},
                               ensure_ascii=False))]
    action = str(args.get("action", "status")).strip()
    try:
        if action == "start":
            out = net_core.start(
                listen=str(args.get("listen", "")).strip(),
                target=str(args.get("target", "127.0.0.1:80")).strip(),
                delay=float(args.get("delay", 0) or 0),
                loss=float(args.get("loss", 0) or 0),
                reorder=float(args.get("reorder", 0) or 0),
                bandwidth=int(args.get("bandwidth", 0) or 0),
            )
        elif action == "stop":
            out = net_core.stop(str(args.get("listen", "")).strip())
        elif action == "sanity":
            out = net_core.sanity(
                delay=float(args.get("delay", 0) or 0),
                loss=float(args.get("loss", 0) or 0),
                reorder=float(args.get("reorder", 0) or 0),
                bandwidth=int(args.get("bandwidth", 0) or 0),
            )
        elif action == "status":
            out = net_core.status()
        else:
            # spec/07.2：其他值 MUST 报参数非法（不得静默回退 status）
            out = {"ok": False, "error": f"参数非法: action={action}（start/stop/status/sanity）"}
    except (TypeError, ValueError) as e:
        out = {"ok": False, "error": f"参数非法: {e}"}
    if out is None:
        out = {"ok": False,
               "error": "rx-net 不可用（未编译或 RX_NET=0）"}
    return [_TC(json.dumps(out, ensure_ascii=False, indent=1))]


def _tool_telemetry_query(args: dict) -> "list[types.TextContent]":
    """遥测记录查询（Rust 端流式读尾部——GB 级日志不整载内存）。"""
    try:
        from telemetry_core import tail
    except ImportError:
        return [_TC(json.dumps({"ok": False,
                                "error": "telemetry_core 不可用"},
                               ensure_ascii=False))]
    try:
        n = min(max(int(args.get("limit", 20)), 1), 200)
    except (TypeError, ValueError):
        n = 20
    tool = str(args.get("tool", "")).strip()
    status_f = str(args.get("status", "")).strip()
    recs = tail(n) or []
    if tool:
        recs = [r for r in recs if r.get("tool") == tool]
    if status_f:
        recs = [r for r in recs if r.get("status") == status_f]
    out = {"ok": True, "count": len(recs), "records": recs}
    return [_TC(json.dumps(out, ensure_ascii=False, indent=1))]


def _tool_mesh_check(args: dict) -> "list[types.TextContent]":
    """网格拓扑健康报告（TetSphere 概念：非流形/破面/孤立顶点）。"""
    from geometry_tools import mesh_check
    p = _check_path(str(args.get("path", "")))
    repair = bool(args.get("repair", False))
    return [_TC(json.dumps(mesh_check(str(p), repair),
                           ensure_ascii=False, indent=2))]


def _tool_mesh_optimize(args: dict) -> "list[types.TextContent]":
    """网格精简建议（NURBS 概念：welding+共面合并）。"""
    from geometry_tools import mesh_optimize
    p = _check_path(str(args.get("path", "")))
    ratio = float(args.get("target_ratio", 0.5))
    return [_TC(json.dumps(mesh_optimize(str(p), ratio),
                           ensure_ascii=False, indent=2))]


def _tool_mesh_splat(args: dict) -> "list[types.TextContent]":
    """三角面片→可训练参数表（Triangle Splatting 概念）。"""
    from geometry_tools import mesh_splat
    p = _check_path(str(args.get("path", "")))
    return [_TC(json.dumps(mesh_splat(str(p)), ensure_ascii=False, indent=2))]


def _tool_voxelize(args: dict) -> "list[types.TextContent]":
    """网格体素化（Radiant Foam 概念：体素占用表示）。"""
    from geometry_tools import voxelize
    p = _check_path(str(args.get("path", "")))
    res = int(args.get("resolution", 16))
    return [_TC(json.dumps(voxelize(str(p), res),
                           ensure_ascii=False, indent=2))]


def _tool_geometry_exchange(args: dict) -> "list[types.TextContent]":
    """格式间直接几何交换（Rhino.Inside 概念：无中间文件）。"""
    from geometry_tools import geometry_exchange
    p = _check_path(str(args.get("path", "")))
    fmt = str(args.get("target_format", "obj"))
    return [_TC(json.dumps(geometry_exchange(str(p), fmt),
                           ensure_ascii=False, indent=2))]


def _tool_half_edge(args: dict) -> "list[types.TextContent]":
    """半边数据结构分析（Manifold3D 概念）。"""
    from geometry_tools import half_edge_analyze
    p = _check_path(str(args.get("path", "")))
    return [_TC(json.dumps(half_edge_analyze(str(p)),
                           ensure_ascii=False, indent=2))]


def _tool_mesh_union(args: dict) -> "list[types.TextContent]":
    """网格并集合并（PicoGK 概念：顶点焊接）。"""
    from geometry_tools import mesh_union
    paths = args.get("paths") or []
    # 安全（security-review LOW）：先限数量再 _check_path（防数万路径先展开）
    if not isinstance(paths, list) or not 1 <= len(paths) <= 10:
        return [_TC(json.dumps({"ok": False,
                                "error": "paths 需 1..10 个网格文件"},
                               ensure_ascii=False))]
    checked = [str(_check_path(str(p))) for p in paths]
    return [_TC(json.dumps(mesh_union(checked),
                           ensure_ascii=False, indent=2))]


def _tool_mesh_clip(args: dict) -> "list[types.TextContent]":
    """平面裁剪（真·CSG 基础：差集操作）。"""
    from geometry_tools import mesh_clip
    p = _check_path(str(args.get("path", "")))
    plane = [float(x) for x in (args.get("plane") or [0, 0, 1, 0])]
    keep = str(args.get("keep", "keep_positive"))
    return [_TC(json.dumps(mesh_clip(str(p), plane, keep),
                           ensure_ascii=False, indent=2))]


def _tool_geom_graph(args: dict) -> "list[types.TextContent]":
    """几何节点图执行（Grasshopper 概念：可视化编程 DSL）。"""
    from geometry_tools import geom_graph
    import geometry_tools as _gt
    _orig = _gt._check_path  # security-review LOW：注入后恢复（防全局污染）
    try:
        _gt._check_path = _check_path  # 注入沙盒（节点图内部路径校验）
        nodes = args.get("nodes") or []
        outputs = args.get("outputs") or []
        return [_TC(json.dumps(geom_graph(nodes, outputs),
                               ensure_ascii=False, indent=2))]
    finally:
        _gt._check_path = _orig


def _tool_geom_example(args: dict) -> "list[types.TextContent]":
    """可运行几何示例生成（PicoGK Program.cs 概念）。"""
    from geometry_tools import geom_example
    kind = str(args.get("kind", "union"))
    return [_TC(json.dumps(geom_example(kind),
                           ensure_ascii=False, indent=2))]


def _tool_patch_learn(args: dict) -> "list[types.TextContent]":
    """补丁学规则（KNighter 概念：diff 提取模式 → 检测规则）。"""
    from patch_learn import patch_learn
    diff_text = str(args.get("diff", ""))
    lang = str(args.get("language", ".py"))
    return [_TC(json.dumps(patch_learn(diff_text, lang),
                           ensure_ascii=False, indent=2))]


def _tool_half_edge_adjacency(args: dict) -> "list[types.TextContent]":
    """半边邻接查询（Manifold3D 概念升级：拓扑操控接口）。"""
    from geometry_tools import half_edge_adjacency
    p = _check_path(str(args.get("path", "")))
    v = int(args.get("vertex", 0))
    return [_TC(json.dumps(half_edge_adjacency(str(p), v),
                           ensure_ascii=False, indent=2))]


def _tool_mesh_boolean(args: dict) -> "list[types.TextContent]":
    """CSG 布尔检测层（AABB 相交判定 + 面心采样）。"""
    from geometry_tools import mesh_boolean
    paths = args.get("paths") or []
    if not isinstance(paths, list) or len(paths) != 2:
        return [_TC(json.dumps({"ok": False,
                                "error": "paths 需 2 个网格文件"},
                               ensure_ascii=False))]
    checked = [str(_check_path(str(p))) for p in paths]
    op = str(args.get("op", "intersect"))
    return [_TC(json.dumps(mesh_boolean(checked, op),
                           ensure_ascii=False, indent=2))]


def _tool_voxel_surface(args: dict) -> "list[types.TextContent]":
    """表面体素提取（Radiant Foam 概念升级：表面点云）。"""
    from geometry_tools import voxel_surface
    p = _check_path(str(args.get("path", "")))
    res = int(args.get("resolution", 16))
    return [_TC(json.dumps(voxel_surface(str(p), res),
                           ensure_ascii=False, indent=2))]


def _brp_query_entities(project_path: str) -> dict | None:
    """BRP 实体查询（2026-08-15 继续处理——深度接入）。

    bevy_remote（Bevy Remote Protocol，localhost:15702）TCP JSON 协议：
    换行分隔 JSON 消息——发 list_entities 请求查实体列表。
    未运行/协议方法未识别 → 返回 None（调用方诚实降级——不崩溃）。
    超时 1.5s（游戏未启动快速降级）。
    方法名可配（风险解决 2026-08-15）：UNIFIED_RX_BRP_METHOD env——
    协议版本差异可适配（默认 list_entities）。
    """
    import socket
    method = os.environ.get("UNIFIED_RX_BRP_METHOD", "list_entities")
    try:
        s = socket.create_connection(("127.0.0.1", 15702), timeout=1.0)
        s.settimeout(1.5)
        try:
            req = json.dumps({"method": method, "params": {}}) + "\n"
            s.sendall(req.encode("utf-8"))
            data = b""
            try:
                data = s.recv(65536)
            except socket.timeout:
                return None  # 协议无响应（游戏忙/协议版本差异——降级）
            if not data:
                return None
            text = data.decode("utf-8", errors="replace").strip()
            try:
                parsed = json.loads(text)
            except ValueError:
                return {"method": "list_entities", "raw": text[:200],
                        "count": "parse-error"}
            if isinstance(parsed, list):
                return {"method": "list_entities", "count": len(parsed),
                        "entities": [str(e)[:80] for e in parsed[:10]]}
            if isinstance(parsed, dict):
                if "error" in parsed:
                    return {"method": "list_entities",
                            "count": "error",
                            "error": str(parsed["error"])[:100]}
                return {"method": "list_entities", "count": "?",
                        "response": str(parsed)[:200]}
            return {"method": "list_entities", "count": "?", "raw": text[:200]}
        finally:
            try:
                s.close()
            except OSError:  # 尽力而为（socket 关闭失败不影响结果）
                pass
    except OSError:
        return None  # 未运行（连接失败）——降级


def _tool_runtime_state(args: dict) -> "list[types.TextContent]":
    import scan_log_core
    p = str(_check_path(str(args.get("path", ""))))
    source = str(args.get("source", "file"))
    state = args.get("state")
    # 降级路径：BRP 不可用时记录文件级状态（最新 mtime 指纹）
    if source == "bevy_brp":
        brp = _brp_query_entities(p)
        if brp is None:
            # 诚实降级：BRP 未运行——记录降级（不崩溃）
            scan_log_core.append_scan({
                "tool": "runtime_state", "root": p, "ok": False,
                "summary": f"BRP localhost:15702 未运行（降级 file 状态——"
                           f"游戏未启动或未启 bevy_remote）"})
            return [_TC(json.dumps({
                "ok": False, "source": "bevy_brp", "degraded": True,
                "note": "BRP 未运行——已记录降级；启动游戏（含 bevy_remote "
                        "插件）后重试", "log": "runtime_state 降级记录"}, 
                ensure_ascii=False, indent=2))]
        # BRP 深度接入（2026-08-15 继续处理）：实体查询成功——记录实体状态
        scan_log_core.append_scan({
            "tool": "runtime_state", "root": p, "ok": True,
            "summary": f"BRP 实体查询: {brp.get('count', '?')} 个实体"
                       f"（{str(brp.get('method', ''))[:30]}）"})
        return [_TC(json.dumps({
            "ok": True, "source": "bevy_brp", "entities": brp,
            "log": "runtime_state 已入 scan-log（BRP 实体状态——"
                   "对话可查最新运行反馈）"},
            ensure_ascii=False, indent=2))]
    # file 来源：记录文件指纹状态
    summary_parts = []
    if isinstance(state, dict):
        summary_parts.append(f"状态 {len(state)} 项")
    else:
        try:
            st = os.stat(p if os.path.isfile(p) else ".")
            summary_parts.append(f"指纹 mtime={st.st_mtime_ns}")
        except OSError:
            summary_parts.append("路径不可读")
    scan_log_core.append_scan({
        "tool": "runtime_state", "root": p, "ok": True,
        "summary": f"runtime {source}: {'; '.join(summary_parts)}"})
    return [_TC(json.dumps({"ok": True, "source": source,
                            "path": p, "state": state,
                            "log": "runtime_state 已入 scan-log（双向反馈——"
                                   "对话可查最新运行状态）"},
                           ensure_ascii=False, indent=2))]


def _tool_ide_actions(args: dict) -> "list[types.TextContent]":
    from ide_tools import ide_actions
    return [_TC(json.dumps(ide_actions(args.get("path", "")), ensure_ascii=False, indent=2))]


def _tool_ide_fusion(args: dict) -> "list[types.TextContent]":
    """IDE 融合：annotate（诊断→符号图聚合，默认）/ impact（双引擎影响面校验）。"""
    action = args.get("action", "annotate")
    path = args.get("path", "")
    _t0 = time.perf_counter()
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
    except ImportError:  # 尽力而为
        pass
    return [_TC(json.dumps(annotate_issues(path, issues), ensure_ascii=False, indent=2))]


def _tool_ide_quest(args: dict) -> "list[types.TextContent]":
    """Quest 状态机：new/resume/status/step/list/abort/note。"""
    from ide_quest import Quest, new_quest, resume_quest, list_quests, STEPS
    action = args.get("action", "status")
    quest_id = str(args.get("quest_id", ""))
    try:
        if action == "new":
            # IDE 增强六十三前序：quest_id 空自动生成（防调用方忘传——
            # 空 id 会让任务文件互相覆盖）
            if not quest_id:
                quest_id = f"q{time.time_ns()}{os.getpid()}"  # 纳秒防同秒碰撞
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
            # 2026-08-14 修复：step 名校验（原忽略 args['step']——传错步名
            # 静默前进误导调用方；现校验与当前步一致）
            _want = str(args.get("step", ""))
            _cur = q.current_step_name()
            if _want and _want != _cur:
                return [_TC(json.dumps(
                    {"ok": False,
                     "error": f"步骤不匹配：当前应完成 {_cur}（收到 {_want}）"},
                    ensure_ascii=False, indent=2))]
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
            if not step:
                # IDE 增强七十一：无 step → 全量步摘要（一步看全链 result）
                _all = {name: {"done": v.get("done", False),
                               "result": v.get("result")}
                        for name, v in q.state.get("steps", {}).items()}
                return [_TC(json.dumps({"ok": True, "quest_id": quest_id,
                                        "step_count": len(_all), "steps": _all},
                                       ensure_ascii=False, indent=2))]
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
            # IDE 增强六十二：std_check 基线对照（工程标准双引擎验证——
            # 修复后 std 分布也应下降）
            _std_prev = q.state.get("std_check") or {}
            _std_prev_counts = _std_prev.get("severity_counts", {}) or {}
            _std_now = {}
            try:
                _std_now = json.loads(_call("std_check", {"path": file})[0].text).get(
                    "severity_counts", {}) or {}
            except Exception:  # 尽力而为
                pass
            _std_total_prev = sum(_std_prev_counts.values())
            _std_total_now = sum(_std_now.values())
            _std_verdict = ("工程标准改善" if _std_total_now < _std_total_prev
                            else "工程标准未变化" if _std_total_now == _std_total_prev
                            else "工程标准恶化")
            return [_TC(json.dumps({"ok": True, "file": file,
                                    "issue_count": cur,
                                    "prev_issue_count": prev_count,
                                    "severity_counts": scan_data.get("severity_counts", {}),
                                    "verdict": verdict,
                                    "fix_scope": _scope,
                                    "std_verdict": _std_verdict,
                                    "std_counts": _std_now,
                                    "std_prev_counts": _std_prev_counts,
                                    "advice": ("对比上次 auto diagnose 问题数：减少=修复生效；"
                                               "未变化=复查 fix 步 checklist/fs_template"
                                               + (f"（期望修改范围 {_scope}）" if _scope else "")
                                               + f"；{_std_verdict}（{_std_total_prev}→{_std_total_now}）")},
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
                    # IDE 增强六十六：std 文件级分布显示
                    _sfs = r.get("std_file_severity") or {}
                    if _sfs:
                        lines.append(f"- 工程标准（该文件）："
                                     f"Critical {_sfs.get('Critical', 0)} / "
                                     f"Error {_sfs.get('Error', 0)} / "
                                     f"Warning {_sfs.get('Warning', 0)}")
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
                    # IDE 增强六十七：fix_scope 显示（修复前 diff 摘要）
                    if r.get("fix_scope"):
                        lines.append(f"- 修复范围：{r['fix_scope']}")
                    # IDE 增强四十：checklist 完整显示（不再截断前 5 项）
                    for c in (r.get("checklist") or []):
                        lines.append(f"- {c}")
                elif name == "lesson":
                    # IDE 增强五十一：lesson 附修复工作量（report 完整文案）
                    _fc = r.get("fix_count", 0)
                    lines.append(f"- {r.get('advice', '')}"
                                 + (f"（本链 {_fc} 条修复建议）" if _fc else ""))
                    # IDE 增强七十六：recall 联动提示（防复发闭环进报告）
                    _rc = r.get("recall") or {}
                    if _rc:
                        lines.append(f"- 防复发：`{_rc.get('tool', '')}`"
                                     f"（任务：{str(_rc.get('args', {}).get('task_description', ''))[:30]}）")
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
            listed = list_quests()
            quests = listed[0]["quests"] if listed and isinstance(listed[0], dict) \
                else listed
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
            _act = sum(1 for q in quests
                       if not q["finished"] and not q.get("aborted")) \
                if status_filter == "all" else len(quests)
            return [_TC(json.dumps({"ok": True, "filter": status_filter,
                                    "quests": quests, "count": len(quests),
                                    "active_count": _act},
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
                            except OSError:  # 尽力而为
                                pass
            return [_TC(json.dumps({"ok": True, "removed": removed,
                                    "note": "已清理 finished/aborted 任务（days>0 时只删过期；active 保留）"},
                                   ensure_ascii=False, indent=2))]
        if action == "auto":
            # IDE 增强七：端到端自动推进链（2026-08-15 六步编排拆出——
            # quest_auto.run_auto——_tool_ide_quest CC=164 瘦身第二刀）
            from quest_auto import run_auto
            return [_TC(json.dumps(run_auto(args, quest_id),
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
    # IDE 增强七十四：键名双兼容（root|path、goal|query——调用方混淆键名
    # 会静默失败，与 ide_complete 键名坑同类）
    root = args.get("root") or args.get("path", "")
    goal = args.get("goal") or args.get("query", "")
    _t0 = time.perf_counter()
    # 安全（2026-08-14 深查）：budget/max_depth 上限校验——
    # 原无上限，budget=10⁹ 会卡死搜索循环（DoS）；负数/0 无意义
    budget = int(args.get("budget", 20))
    max_depth = int(args.get("max_depth", 4))
    if not 1 <= budget <= 5000:
        raise ValueError(f"budget 须在 1..5000（收到 {budget}）")
    if not 1 <= max_depth <= 20:
        raise ValueError(f"max_depth 须在 1..20（收到 {max_depth}）")
    # IDE 增强 170（安全）：root 过 _check_path 沙盒校验——
    # 防越界目录树搜索（读取沙盒外文件内容进探索）
    root = _check_path(root)
    if not root or not os.path.isdir(root):
        return [_TC(json.dumps({"ok": False, "error": f"目录不存在: {root}"}, ensure_ascii=False))]
    if not goal:
        return [_TC(json.dumps({"ok": False, "error": "需要 goal（探索目标描述）"}, ensure_ascii=False))]
    from explore_engine import normalize_goals  # 2026-08-15 拆出（_SYN 词表域）
    goals = normalize_goals(goal)

    # 预扫：目标词相关文件作为起始候选
    import re as _re
    candidates: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ("target", "node_modules", ".git", "release")]
        for fn in filenames:
            if not fn.lower().endswith(tuple(_ANALYZE_EXTS)):
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
        # 语义检索兜底（阶段4）：关键词未命中 → rx-search BM25 语义补充
        try:
            from search_core import search as _sem_search
            sr = _sem_search(goal, str(root), 10)
            if sr and sr.get("hits"):
                return [_TC(json.dumps({
                    "ok": True, "query": goal, "mode": "semantic_fallback",
                    "hits": [{"path": h.get("path", ""),
                              "line": h.get("line", 0),
                              "score": h.get("score", 0),
                              "symbol": h.get("symbol")}
                             for h in sr["hits"][:10]],
                    "hint": "关键词未命中——已用语义检索兜底（BM25+符号加权）",
                }, ensure_ascii=False, indent=2))]
        except Exception:  # noqa: BLE001 —— 语义兜底失败维持原错误
            pass
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
    # IDE 增强 202：命中密度（所有候选文件里目标词的命中行总数——
    # 目标词在仓库的分布一眼可见）
    try:
        _words2 = [w for w in goal.lower().replace(",", " ").split() if len(w) > 1]
        _total_hits = 0
        for _c in candidates:
            _cp = str(_c)
            if not os.path.isfile(_cp):
                continue
            with open(_cp, encoding="utf-8", errors="replace") as _cf:
                for _ln in _cf:
                    if any(w in _ln.lower() for w in _words2):
                        _total_hits += 1
        result["total_hits"] = _total_hits
    except Exception:  # 尽力而为
        result["total_hits"] = 0
    # IDE 增强 151：探索结果提示（best 文件 + 命中密度）
    _best = result.get("best", "")
    # IDE 增强 289：探索语言分布（候选文件后缀——AI 知道目标代码的语言）
    _expl_langs: dict[str, int] = {}
    for _c in candidates:
        _sfx = os.path.splitext(str(_c))[1].lower().lstrip(".")
        if _sfx:
            _expl_langs[_sfx] = _expl_langs.get(_sfx, 0) + 1
    result["languages"] = dict(sorted(_expl_langs.items(), key=lambda kv: -kv[1]))
    if _best:
        # 先 basename 再剥 :行号（Windows 盘符在 basename 前已被消化——
        # 对完整路径做任何 split(':') 都会切掉盘符，探针两轮抓出）
        _bn = os.path.basename(str(_best)).rsplit(":", 1)[0]
        result["advice"] = (f"最优候选：{_bn}"
                            f"（深度 {result.get('stats', {}).get('depth_reached', '?')}）"
                            f"——先读该文件再决策")
        # IDE 增强 181：best 命中行 snippet（探索结果可读性——不用再
        # 手动读文件就知命中在哪；best 常态为纯路径，先试完整路径
        # 再剥 :行号——Windows 盘符坑：必须先 isfile 成功）
        try:
            _fpath_full = str(_best)
            _lno = ""
            if not os.path.isfile(_fpath_full):
                _fpath_full, _lno = _fpath_full.rsplit(":", 1)
            if os.path.isfile(_fpath_full):
                with open(_fpath_full, encoding="utf-8", errors="replace") as _bf:
                    _blines = _bf.read().splitlines()
                _i = int(_lno) - 1 if _lno.isdigit() else -1
                if not (0 <= _i < len(_blines)):
                    _words = [w for w in goal.lower().replace(",", " ").split()
                              if len(w) > 1]
                    _i = next((k for k, ln in enumerate(_blines)
                               if any(w in ln.lower() for w in _words)), -1)
                if 0 <= _i < len(_blines):
                    result["best_hit"] = _blines[_i].strip()[:160]
                    # IDE 增强 211：命中上下文（前后各 1 行——命中场景更清楚）
                    _ctx = []
                    for _k in (_i - 1, _i, _i + 1):
                        if 0 <= _k < len(_blines):
                            _ctx.append(f"{_k + 1}: {_blines[_k].strip()[:80]}")
                    result["best_context"] = _ctx
        except Exception:  # 尽力而为
            pass
    else:
        result["advice"] = "未找到高相关候选——换关键词或扩大 root"
    # IDE 增强 234：探索耗时（ms——性能可见收官）
    result["elapsed_ms"] = round((time.perf_counter() - _t0) * 1000, 1)
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
    # IDE 增强 171（安全）：root/db 过 _check_path 沙盒校验（防任意 .db
    # 读取/越界索引——与 kb_query 169 同族）
    root = _check_path(root)
    db_path = args.get("db", os.path.join(root or ".", ".unified-rx-index", "semantic.db"))
    if db_path:
        db_path = _check_path(db_path)
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
            if not fn.lower().endswith(tuple(_ANALYZE_EXTS)):
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
        _t0 = time.perf_counter()
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
        except ImportError:  # 尽力而为
            pass
        results = results[:limit]
    except Exception as e:
        return [_TC(json.dumps({"ok": False, "error": f"检索失败: {e}"}, ensure_ascii=False))]
    return [_TC(json.dumps({"ok": True, "query": query, "indexed_files": added,
                            "results": results,
                            # IDE 增强 216：检索耗时（ms——对称 kb_query 215）
                            "elapsed_ms": round((time.perf_counter() - _t0) * 1000, 1),
                            "note": ("BM25+向量 RRF 混合检索（bge-small-zh 本地 embedding）" if vector_used
                                     else "BM25 全文检索（无 embedding 模型——纯 BM25 降级）"),
                            # IDE 增强 194：命中质量建议（对称 kb_query 150）
                            "advice": (f"命中 {len(results)} 条（索引 {added} 文件）——"
                                       f"首个 {str(results[0].get('title', ''))[:20]}"
                                       if results else "0 命中——换关键词，或确认 root 含代码文件")},
                           ensure_ascii=False, indent=2))]


def _tool_local_intel(args: dict) -> "list[types.TextContent]":
    """本地智能：status/embed/similarity。"""
    from local_intel import LocalIntel
    li = LocalIntel()
    action = args.get("action", "status")
    _t0 = time.perf_counter()
    if action == "status":
        return [_TC(json.dumps({"ok": True, "available": li.available(),
                                "models_dir": str(li._dir),
                                # IDE 增强 238：处理耗时（ms——性能可见收官）
                                "elapsed_ms": round((time.perf_counter() - _t0) * 1000, 1)},
                               ensure_ascii=False, indent=2))]
    if action == "embed":
        v = li.embed(args.get("text", ""))
        if v is None:
            return [_TC(json.dumps({"ok": False, "error": "embedding 模型不可用（模型缺失或推理失败）"},
                                   ensure_ascii=False))]
        return [_TC(json.dumps({"ok": True, "dim": len(v),
                                "vector_preview": v[:8], "norm": round(sum(x*x for x in v) ** 0.5, 4),
                                "elapsed_ms": round((time.perf_counter() - _t0) * 1000, 1)},
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
    # 核心合并：组合工具剥离外层 action 后，内层子动作经 sub_action 透传
    action = args.get("action") or args.get("sub_action", "state")
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
    """项目本质三分（settled/adjustable/doubts）+ 智能体调用留痕/相似性检查。

    action=add|get|search|list（三分笔记）|trace（项目内智能体调用留痕：
    agent/action/detail → <root>/.unified-rx/traces.jsonl）|traces（读取留痕，
    agent 过滤）|similar（相似性检查：query 在项目笔记+留痕+跨智能体聊天
    记录中查相似——搞项目前看看有没有其他智能体搞过相似的）。
    """
    from design_notes import (add_note, list_notes, get_note,
                              trace_call, list_traces, similar_notes)
    action = args.get("action", "list")
    # IDE 增强 184（安全）：root 过 _check_path（add 写
    # <root>/design_notes.json——任意路径写文件越界；转 str 防 WindowsPath）
    root = str(_check_path(str(args.get("root", ""))))
    kind = args.get("kind", "")
    _t0 = time.perf_counter()
    try:
        if action == "add":
            r = add_note(root, kind, args.get("text", ""), args.get("tag", ""))
        elif action == "get":
            r = get_note(root, kind)
        elif action == "trace":
            r = trace_call(root, str(args.get("agent", "")),
                           str(args.get("action", "")),
                           str(args.get("detail", "")))
        elif action == "traces":
            r = list_traces(root, agent=str(args.get("agent", "")),
                            limit=int(args.get("limit", 20)))
        elif action == "similar":
            r = similar_notes(root, str(args.get("query", "")),
                              limit=int(args.get("limit", 10)))
        elif action == "search":
            # IDE 增强 182：全文检索（query 在全部 notes 里搜——含 tag）
            q = str(args.get("query", "")).strip().lower()
            r = list_notes(root)
            if r.get("ok"):
                _hits = [n for n in r.get("recent", [])]  # 占位（recent 只 3 条）
                _all = []
                for _k in ("settled", "adjustable", "doubts"):
                    for _n in r.get(_k, []):
                        _all.append({"kind": _k, **{kk: v for kk, v in _n.items()
                                                    if kk != "kind"}})
                _hits = [n for n in _all
                         if not q or q in str(n.get("text", "")).lower()
                         or q in str(n.get("tag", "")).lower()]
                r = {"ok": True, "root": root, "query": args.get("query", ""),
                     "hits": _hits, "hit_count": len(_hits),
                     "advice": f"{len(_hits)} 条命中（text/tag 含 '{args.get('query', '')}'）"}
        else:
            r = list_notes(root)
        # IDE 增强 236：操作耗时（ms——性能可见收官）
        if isinstance(r, dict):
            r["elapsed_ms"] = round((time.perf_counter() - _t0) * 1000, 1)
        return [_TC(json.dumps(r, ensure_ascii=False, indent=2))]
    except Exception as e:
        return [_TC(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"},
                               ensure_ascii=False))]


def _tool_scan_trend(args: dict) -> "list[types.TextContent]":
    """扫描日志趋势分析（M6：日志→统计→增强闭环）。"""
    from scan_trend import analyze
    import scan_log_core as _scl
    _t0 = time.perf_counter()
    try:
        logs = _scl.query_logs(limit=2000)
    except Exception:
        logs = []
    r = analyze(logs, int(args.get("window_days", 7)))
    # IDE 增强 249：分析耗时（ms——收官）
    if isinstance(r, dict):
        r["elapsed_ms"] = round((time.perf_counter() - _t0) * 1000, 1)
    return [_TC(json.dumps(r, ensure_ascii=False, indent=2))]


def _tool_ui_check(args: dict) -> "list[types.TextContent]":
    """Bevy UI 静态检查（程序驱动，非 skill）：扫描 Rust UI 代码的崩溃/不可见模式。
    规则：ui_root_missing/camera_missing/mode_isolation/focus_pass/font_missing/z_ordering。
    目录扫描走沙盒校验 + 上限（max_files 1..500/单文件 1MB）。"""
    p = _check_path(str(args["path"]))
    _t0 = time.perf_counter()
    max_files = int(args.get("max_files", 100))
    if not 1 <= max_files <= 500:
        raise ValueError("max_files 须在 1..500")
    try:
        from ui_check_core import scan_ui_dir, scan_ui_source, _scan_gd_ui, _scan_cs_ui, _scan_dart_ui
    except ImportError:
        # 回退：直接按文件扫描（ui_check_core 与 server.py 同目录）
        _dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, _dir)
        from ui_check_core import scan_ui_dir, scan_ui_source, _scan_gd_ui, _scan_cs_ui, _scan_dart_ui  # noqa: F811
    if p.is_dir():
        issues = scan_ui_dir(str(p), max_files=max_files)
    elif p.is_file() and p.suffix == ".rs":
        if p.stat().st_size > _MAX_READ:
            raise ValueError(f"文件过大（>{_MAX_READ}）")
        src = p.read_text(encoding="utf-8", errors="replace")
        issues = scan_ui_source(src, str(p))
        for i in issues:
            i["file"] = str(p)
    elif p.is_file() and p.suffix == ".gd":
        # IDE 增强 257：Godot UI 单文件（Button 死按钮检查）
        if p.stat().st_size > _MAX_READ:
            raise ValueError(f"文件过大（>{_MAX_READ}）")
        src = p.read_text(encoding="utf-8", errors="replace")
        issues = _scan_gd_ui(src, str(p))
    elif p.is_file() and p.suffix == ".cs":
        # IDE 增强 267：Unity UI 单文件（Button 死按钮检查）
        if p.stat().st_size > _MAX_READ:
            raise ValueError(f"文件过大（>{_MAX_READ}）")
        src = p.read_text(encoding="utf-8", errors="replace")
        issues = _scan_cs_ui(src, str(p))
    elif p.is_file() and p.suffix == ".dart":
        # IDE 增强 274：Flutter UI 单文件（Button 死按钮检查）
        if p.stat().st_size > _MAX_READ:
            raise ValueError(f"文件过大（>{_MAX_READ}）")
        src = p.read_text(encoding="utf-8", errors="replace")
        issues = _scan_dart_ui(src, str(p))
    else:
        raise ValueError(f"仅支持 .rs/.gd/.cs/.dart 文件或目录: {p}")
    # IDE 增强 286：ui_check 语言画像（从 issues 的 file 后缀统计——
    # 覆盖目录与单文件两种模式）
    _ui_langs: dict[str, int] = {}
    for _i in issues:
        _sfx = os.path.splitext(str(_i.get("file", "")))[1].lower().lstrip(".")
        if _sfx:
            _ui_langs[_sfx] = _ui_langs.get(_sfx, 0) + 1
    if not _ui_langs and p.is_file():
        _ui_langs[p.suffix.lower().lstrip(".")] = 1
    # IDE 增强 180（里程碑）：规则过滤（rules 逗号分隔——扫描工具全补齐，
    # 对称 std_check 173/bug_scan 174/cb_scan 175）
    _only = {s.strip() for s in str(args.get("rules", "")).split(",") if s.strip()}
    if _only:
        issues = [i for i in issues if i.get("rule") in _only]
    # P2 对齐（2026-08-13）：severity 归一化统计 + noise_ratio（与 bug_scan 一致）——
    # AI 可判断报告可信度；ui_check_core 产出 error/warning，归一化到 error/warn/info
    sev_counts = {"error": 0, "warn": 0, "info": 0}
    for i in issues:
        s = str(i.get("severity", "warning"))
        sev_counts["warn" if s in ("warn", "warning") else
                   ("error" if s == "error" else "info")] += 1
    total = len(issues)
    # IDE 增强 142：规则分布（什么 UI 问题类型最多）
    _rule_counts: dict[str, int] = {}
    for i in issues:
        _r = str(i.get("rule", "unknown"))
        _rule_counts[_r] = _rule_counts.get(_r, 0) + 1
    # IDE 增强 203：问题最多文件（worst_file 用）
    _worst_f, _worst_n = "", 0
    if issues:
        _fc: dict[str, int] = {}
        for i in issues:
            _k = str(i.get("file", ""))
            _fc[_k] = _fc.get(_k, 0) + 1
        _worst_f, _worst_n = max(_fc.items(), key=lambda kv: kv[1])
    # IDE 增强 213：最多问题规则（top_rule——对称 std 209/bug 210，
    # UI 三入口收官）
    if _rule_counts:
        _tr, _tn = max(_rule_counts.items(), key=lambda kv: kv[1])
        _top_rule = f"{_tr}（{_tn} 条）"
    else:
        _top_rule = ""
    # IDE 增强 220（里程碑）：文件级规则分布（file_rules——对称 std 219）
    _fr: dict[str, dict[str, int]] = {}
    for i in issues:
        _f = os.path.basename(str(i.get("file", "")))
        _r = str(i.get("rule", "unknown"))
        _fr.setdefault(_f, {})
        _fr[_f][_r] = _fr[_f].get(_r, 0) + 1
    _file_rules = dict(sorted(_fr.items())[:20])
    # IDE 增强 188：可用规则列表（rules= 可传哪些——四大单入口收官）
    _ar = sorted(set(_rule_counts) | {"ui_root_missing", "camera_missing",
                                      "mode_isolation", "focus_pass",
                                      "font_missing", "z_ordering",
                                      "no_interaction"})
    # IDE 增强 282：规则引擎来源标注（no_interaction 覆盖四引擎）
    _eng = {"ui_root_missing": "bevy", "camera_missing": "bevy",
            "mode_isolation": "bevy", "focus_pass": "bevy",
            "font_missing": "bevy", "z_ordering": "bevy",
            "no_interaction": "bevy/godot/unity/flutter"}
    return [_TC(json.dumps({
        "ok": True, "issue_count": len(issues),
        "rule_engines": _eng,
        "severity_counts": sev_counts,
        "noise_ratio": round(sev_counts["info"] / total, 3) if total else 0.0,
        "rule_counts": dict(sorted(_rule_counts.items(), key=lambda kv: -kv[1])),
        "available_rules": _ar,
        # IDE 增强 286：ui_check 语言画像（扫描后缀分布——四引擎一眼可见；
        # vuln/project 聚合认 languages 字段）
        "languages": dict(sorted(_ui_langs.items(), key=lambda kv: -kv[1])),
        # IDE 增强 146：最严重 UI 问题提示（error 级优先——UI 崩溃/不可见）
        "advice": (f"最优先：{os.path.basename(str(issues[0].get('file', '')))}:"
                   f"{issues[0].get('line')} [{issues[0].get('rule')}] "
                   f"{str(issues[0].get('msg', ''))[:40]}"
                   if issues else "无 UI 问题"),
        # IDE 增强 203：问题最多文件（集中修复入口——先处理最脏文件）
        "worst_file": (f"{os.path.basename(str(_worst_f))}（{_worst_n} 条问题）"
                       if issues else ""),
        "top_rule": _top_rule,
        "file_rules": _file_rules,
        "note": "severity 已归一化（warning→warn）；noise_ratio=info 占比",
        "issues": issues,
        # IDE 增强 228：扫描耗时（ms——扫描四入口收官 std 226/bug 227/ui 228）
        "elapsed_ms": round((time.perf_counter() - _t0) * 1000, 1),
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



from mcp import types

def _tool_scan_now(args: dict) -> "list[types.TextContent]":
    """常态扫描：一键 vuln_scan + bug_scan（写完代码立刻挖——不等到收尾）。
    path 默认自动探测（git 根/cwd）。返回聚合 JSON。"""
    import os as _os
    path = args.get("path") or _os.getcwd()
    if not _os.path.isdir(path):
        path = _os.path.dirname(path) or "."
    out = {"path": path, "ok": True}
    try:
        r = _tool_vuln_scan({"path": path, "max_files": int(args.get("max_files", 100))})
        t = r[0].text if isinstance(r, list) else str(r)
        out["vuln_scan"] = json.loads(t) if t.startswith("{") else {"raw": t[:200]}
    except Exception as e:  # noqa: BLE001
        out["vuln_scan"] = {"error": str(e)}
        out["ok"] = False
    try:
        r = _tool_bug_scan({"path": path, "max_files": int(args.get("max_files", 100))})
        t = r[0].text if isinstance(r, list) else str(r)
        out["bug_scan"] = json.loads(t) if t.startswith("{") else {"raw": t[:200]}
    except Exception as e:  # noqa: BLE001
        out["bug_scan"] = {"error": str(e)}
        out["ok"] = False
    return [types.TextContent(type="text", text=json.dumps(out, ensure_ascii=False))]


from mcp import types

def _tool_scan_delta(args: dict) -> "list[types.TextContent]":
    """增量扫描：git diff 变更文件 → 只扫变更（快——写完一个文件立刻扫）。"""
    import os as _os
    import subprocess as _sp
    path = args.get("path") or _os.getcwd()
    # 取 git diff 变更文件（未提交 + 未暂存）
    try:
        r = _sp.run(["git", "-C", path, "diff", "--name-only", "HEAD"], capture_output=True,
                    text=True, timeout=60)
        files = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
        if not files:
            r = _sp.run(["git", "-C", path, "status", "--short"], capture_output=True,
                        text=True, timeout=30)
            files = [ln[3:].strip() for ln in r.stdout.splitlines() if ln.strip()]
    except Exception as e:  # noqa: BLE001
        return [types.TextContent(type="text", text=json.dumps(
            {"ok": False, "error": f"git 不可用: {e}"}, ensure_ascii=False))]
    if not files:
        return [types.TextContent(type="text", text=json.dumps(
            {"ok": True, "delta": [], "note": "无变更文件"}, ensure_ascii=False))]
    # 只扫变更文件
    out = {"ok": True, "delta_files": files[:50], "issues": []}
    for f in files[:50]:
        fp = _os.path.join(path, f)
        if not _os.path.isfile(fp):
            continue
        try:
            r = _tool_bug_scan({"path": fp, "max_files": 5})
            t = r[0].text if isinstance(r, list) else str(r)
            d = json.loads(t) if t.startswith("{") else {}
            iss = d.get("issues") or d.get("findings") or []
            if iss:
                out["issues"].append({"file": f, "issues": iss})
        except Exception as e:  # noqa: BLE001
            out["issues"].append({"file": f, "error": str(e)})
    return [types.TextContent(type="text", text=json.dumps(out, ensure_ascii=False))]


from mcp import types

def _tool_git_bisect_find(args: dict) -> "list[types.TextContent]":
    """可回溯：git bisect 自动二分定位引入 bug 的提交。
    test_cmd 为判定命令（0=好，非0=坏）；bisect 自动跑并输出首次坏提交。"""
    import os as _os
    import subprocess as _sp
    path = args.get("path") or _os.getcwd()
    good = args.get("good")  # 好提交（无 bug）
    bad = args.get("bad") or "HEAD"  # 坏提交（默认 HEAD）
    test_cmd = args.get("test_cmd", "cargo test")
    if not good:
        return [types.TextContent(type="text", text=json.dumps(
            {"ok": False, "error": "需提供 good 提交（已知无 bug 的提交）"}, ensure_ascii=False))]
    # 安全校验（常态扫描告警修复）：test_cmd 白名单——只允许已知测试命令
    _ALLOWED = ("cargo test", "cargo check", "python -m pytest", "pytest", "node --test",
                "npm test", "go test", "go vet")
    if not test_cmd.startswith(_ALLOWED):
        return [types.TextContent(type="text", text=json.dumps(
            {"ok": False, "error": f"test_cmd 不在白名单: {_ALLOWED}"}, ensure_ascii=False))]
    log = []
    try:
        _sp.run(["git", "-C", path, "bisect", "reset"], capture_output=True, timeout=30)
        _sp.run(["git", "-C", path, "bisect", "start"], capture_output=True, timeout=30)
        _sp.run(["git", "-C", path, "bisect", "bad", bad], capture_output=True, timeout=30)
        _sp.run(["git", "-C", path, "bisect", "good", good], capture_output=True, timeout=30)
        step = 0
        while step < 40:  # 最多 40 轮（足够二分）
            step += 1
            # 当前检查点提交
            r = _sp.run(["git", "-C", path, "rev-parse", "--short", "HEAD"],
                        capture_output=True, text=True, timeout=30)
            rev = r.stdout.strip()
            # 跑测试命令
            import shlex
            tr = _sp.run(shlex.split(test_cmd), cwd=path, capture_output=True, text=True, timeout=900)
            verdict = "good" if tr.returncode == 0 else "bad"
            log.append(f"[{step}] {rev}: {verdict}")
            _sp.run(["git", "-C", path, "bisect", verdict], capture_output=True, timeout=30)
            # bisect 结束判定：输出 "is the first bad commit"
            sr = _sp.run(["git", "-C", path, "bisect", "log"], capture_output=True,
                         text=True, timeout=30)
            if "first bad commit" in sr.stdout or "is the first bad commit" in sr.stderr:
                break
            # 无更多候选（退出码 128 或输出确认）
            rr = _sp.run(["git", "-C", path, "bisect", "visualize"], capture_output=True,
                         text=True, timeout=30)
            if not rr.stdout.strip() and step > 3:
                break
        # 读取首个坏提交
        lr = _sp.run(["git", "-C", path, "bisect", "log"], capture_output=True, text=True, timeout=30)
        first_bad = ""
        for ln in lr.stdout.splitlines() + lr.stderr.splitlines():
            if "first bad commit" in ln:
                parts = ln.split()
                first_bad = parts[-1] if parts else ""
                break
        _sp.run(["git", "-C", path, "bisect", "reset"], capture_output=True, timeout=30)
        return [types.TextContent(type="text", text=json.dumps({
            "ok": True, "first_bad_commit": first_bad, "log": log,
            "note": "测试命令: " + test_cmd}, ensure_ascii=False))]
    except Exception as e:  # noqa: BLE001
        try:
            _sp.run(["git", "-C", path, "bisect", "reset"], capture_output=True, timeout=30)
        except Exception as re:  # noqa: BLE001
            log.append(f"bisect reset 失败: {re}")
        return [types.TextContent(type="text", text=json.dumps(
            {"ok": False, "error": str(e), "log": log}, ensure_ascii=False))]


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


def _tool_repo_health(args: dict) -> "list[types.TextContent]":
    """代码库健康四理念（去重/剔残缺/分支/标矛盾，用户 2026-08-17 理念）。

    action=dedup|incomplete|branch|conflict|all（必填）；
    root=项目根（默认 UNIFIED_RX_PROJECT 或当前目录）；top=结果上限。
    只读检测：去重（完全相同/近似/重复块）、剔残缺（空实现/TODO/断引用）、
    分支健康（git，非 git 降级）、标矛盾（同名符号）。
    返回 {ok, action, root, items, summary, score, elapsed_ms}。
    """
    action = str(args.get("action", ""))
    if action not in ("dedup", "incomplete", "branch", "conflict", "all"):
        raise ValueError("action 必填: dedup/incomplete/branch/conflict/all")
    root = str(args.get("root", os.environ.get("UNIFIED_RX_PROJECT") or os.getcwd()))
    top = int(args.get("top", 20))
    if not 1 <= top <= 200:
        raise ValueError("top 须在 1..200")
    try:
        from repo_health import repo_health
    except ImportError:
        _dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, _dir)
        from repo_health import repo_health  # noqa: F811
    return [_TC(json.dumps(repo_health(action, root, top), ensure_ascii=False))]


def _tool_cost_report(args: dict) -> "list[types.TextContent]":
    """成本核算（用户 2026-08-17：每个代码/工具调用次数和 token 消耗成本都要算）。

    action=summary（默认，按工具/天/项目汇总调用次数+token+成本）|
           estimate（估算一段文本的 token 与成本，text 参数）|
           code（估算代码文件/目录的 token 与成本，path 参数）
    model=模型单价表键（deepseek-chat 默认/deepseek-reasoner/claude-sonnet/
          claude-opus/gpt-4o/gpt-4o-mini/qwen-max/qwen-plus）
    summary 数据源：~/.unified-rx/stats.json 自动打点（tokens_in/out 已由 _call 估算）。
    """
    from cost_core import estimate_tokens, estimate_cost, summarize
    action = str(args.get("action", "summary"))
    model = str(args.get("model", "deepseek-chat"))
    if action == "estimate":
        text = str(args.get("text", ""))
        tok = estimate_tokens(text)
        return [_TC(json.dumps({"ok": True, "action": "estimate",
                                "chars": len(text), **estimate_cost(tok, 0, model)},
                               ensure_ascii=False))]
    if action == "code":
        p = _check_path(str(args.get("path", os.getcwd())))
        total_tok = 0
        files = 0
        if p.is_file():
            paths = [p]
        else:
            paths = [f for f in p.rglob("*") if f.is_file() and
                     f.suffix.lower() in {".py", ".js", ".ts", ".tsx", ".jsx", ".rs",
                                          ".go", ".java", ".c", ".cpp", ".h", ".json",
                                          ".md", ".html", ".css", ".vue"}][:500]
        for f in paths:
            try:
                if f.stat().st_size > 5_000_000:
                    continue
                total_tok += estimate_tokens(f.read_text(encoding="utf-8", errors="replace"))
                files += 1
            except OSError:
                continue
        return [_TC(json.dumps({"ok": True, "action": "code", "path": str(p),
                                "files": files, **estimate_cost(total_tok, 0, model)},
                               ensure_ascii=False))]
    if action != "summary":
        raise ValueError("action 可选: summary/estimate/code")
    # summary：读 stats 自动打点记录汇总
    records = []
    try:
        from vendor.extensions.stats.server import _load
        records = _load()
    except Exception:
        state = os.path.join(os.path.expanduser("~"), ".unified-rx", "stats.json")
        try:
            import json as _json
            records = _json.load(open(state, encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return [_TC(json.dumps(summarize(records, model=model), ensure_ascii=False))]


def _tool_chatlog_search(args: dict) -> "list[types.TextContent]":
    """不同智能体聊天记录检索（用户 2026-08-17：一定要获取不同的智能体聊天记录）。

    action=search（默认，query 关键词匹配 title+text，agent 过滤，since_days 限定）
          |collect（重新采集 Marvis/Hermes/Trae/Qoder 的聊天/记忆/编辑留痕，
                   去重追加到 ~/.unified-rx/chatlog.jsonl）|status（索引统计）。
    query/agent/limit/since_days 为 search 参数；agents 为 collect 参数（逗号分隔）。
    """
    from chatlog_core import collect, search
    action = str(args.get("action", "search"))
    if action == "collect":
        agents = [a.strip() for a in str(args.get("agents", "")).split(",") if a.strip()] or None
        return [_TC(json.dumps(collect(agents), ensure_ascii=False))]
    if action == "status":
        try:
            with open(os.path.join(os.path.expanduser("~"), ".unified-rx",
                                   "chatlog.jsonl"), encoding="utf-8") as f:
                n = sum(1 for ln in f if ln.strip())
            return [_TC(json.dumps({"ok": True, "action": "status", "total": n},
                                   ensure_ascii=False))]
        except OSError:
            return [_TC(json.dumps({"ok": True, "action": "status", "total": 0},
                                   ensure_ascii=False))]
    if action != "search":
        raise ValueError("action 可选: search/collect/status")
    limit = int(args.get("limit", 20))
    if not 1 <= limit <= 100:
        raise ValueError("limit 须在 1..100")
    r = search(str(args.get("query", "")),
               agent=str(args.get("agent", "")) or None,
               limit=limit,
               since_days=int(args.get("since_days", 0)) or None)
    return [_TC(json.dumps(r, ensure_ascii=False))]


def _tool_local_tools(args: dict) -> "list[types.TextContent]":
    """本地工具注册表与安全调用桥（用户 2026-08-17：可以调用大部分的本地工具）。

    action=scan（扫描 D:\\rj\\GJ/SJ/KF 等工具根 → 注册表）|
           discover（列出已注册工具，query 名称过滤）|
           run（安全调用：name 工具名 + args 参数列表 + timeout 秒——
                白名单注册 + 危险参数黑名单 + 输出截断 20k）
    LOCAL_TOOL_ROOTS 环境变量可扩展工具根（分号分隔）。
    """
    from local_tools import scan, discover, run
    action = str(args.get("action", "discover"))
    if action == "scan":
        return [_TC(json.dumps(scan(), ensure_ascii=False))]
    if action == "discover":
        r = discover(query=str(args.get("query", "")),
                     category=str(args.get("category", "")))
        return [_TC(json.dumps(r, ensure_ascii=False))]
    if action == "run":
        r = run(str(args.get("name", "")),
                args.get("args") or [],
                timeout=int(args.get("timeout", 60)))
        return [_TC(json.dumps(r, ensure_ascii=False))]
    raise ValueError("action 可选: scan/discover/run")


def _tool_backup(args: dict) -> "list[types.TextContent]":
    """每日备份 + 回溯（用户 2026-08-17：每天备份，备份不会太多 + 回溯效果）。

    action=backup（git commit + tag daily-YYYYMMDD + 限量快照 zip，
           keep 默认 7 份，删最旧）|list（备份时间线）|
           rollback（回溯到指定日期快照，恢复前自动另存当前状态防不可逆）。
    root=项目根（必填）；keep=保留份数（1..30）。
    快照目录：~/.unified-rx/backups/<项目名>/<YYYYMMDD>.zip
    """
    from backup_core import daily_backup, list_snapshots, rollback
    action = str(args.get("action", "list"))
    root = str(args.get("root", ""))
    if action == "backup":
        r = daily_backup(root, keep=int(args.get("keep", 7)))
    elif action == "rollback":
        r = rollback(root, str(args.get("date", "")))
    elif action == "list":
        r = list_snapshots(root)
    else:
        raise ValueError("action 可选: backup/list/rollback")
    return [_TC(json.dumps(r, ensure_ascii=False))]


def _tool_ide_health(args: dict) -> "list[types.TextContent]":
    """IDE 工具族健康自检（用户 2026-08-17：IDE 还是太弱——先诊断弱在哪）。

    检查：graph_index（tree-sitter 符号图）可用性与语言数、LSP server
    （pylsp/rust-analyzer/clangd）是否可发现、ide_cache 缓存条目数、
    ide_tools 模块完整性。输出 {ok, checks: [{name, ok, detail}], advice}。
    """
    checks = []
    # 1) graph_index / tree-sitter
    try:
        import graph_index as gi
        langs = getattr(gi, "SUPPORTED_LANGS", getattr(gi, "LANGS", "unknown"))
        checks.append({"name": "graph_index", "ok": True,
                       "detail": f"符号图可用，支持语言: {langs}"})
    except Exception as e:
        checks.append({"name": "graph_index", "ok": False, "detail": str(e)[:120]})
    # 2) LSP server 可发现性
    import shutil
    for lsp in ("pylsp", "rust-analyzer", "clangd", "typescript-language-server"):
        found = shutil.which(lsp) is not None
        checks.append({"name": f"lsp:{lsp}", "ok": found,
                       "detail": shutil.which(lsp) or "未安装（可在 PATH 安装后启用）"})
    # 3) ide_cache 状态
    try:
        from ide_cache import _CACHE, _MAX_ENTRIES, _WARM_DB
        checks.append({"name": "ide_cache", "ok": True,
                       "detail": f"缓存 {len(_CACHE)}/{_MAX_ENTRIES} 条目，"
                                 f"温层持久化: {'启用' if _WARM_DB else '未启用'}"})
    except Exception as e:
        checks.append({"name": "ide_cache", "ok": False, "detail": str(e)[:120]})
    # 4) ide_tools 完整性
    import ide_tools
    for fn in ("ide_rename", "ide_complete", "ide_references", "ide_actions"):
        checks.append({"name": f"ide_tools.{fn}", "ok": hasattr(ide_tools, fn),
                       "detail": "可用" if hasattr(ide_tools, fn) else "缺失"})
    bad = [c for c in checks if not c["ok"]]
    return [_TC(json.dumps({
        "ok": True, "checks": checks,
        "summary": f"{len(checks) - len(bad)}/{len(checks)} 项健康",
        "advice": ("LSP server 未安装——语义补全/悬停/跳转走 graph_index 降级；"
                   "安装 pylsp（pip install python-lsp-server）可增强 Python 语义"
                   if any(c["name"].startswith("lsp:") and not c["ok"] for c in checks)
                   else "IDE 工具族健康"),
    }, ensure_ascii=False, indent=2))]


def _tool_layer_check(args: dict) -> "list[types.TextContent]":
    """分层开发理念 + 写完即模拟（用户 2026-08-17：先布局再动画再美术；写完要模拟）。

    action=ui（UI 文件三层分检：布局→动画→美术，含顺序违规校验）|
           code（代码三层分检：骨架→逻辑→优化）|
           simulate（写完即模拟：Python AST+py_compile+隔离 import；
                    JS/TS node --check——模拟不通过提示先修再交付）|
           clip（剪辑：粗剪→精剪→调色音效）|anim3d（建模绑定→K帧→渲染）。
    path=目标文件（必填）。
    """
    from layer_check import layer_check
    action = str(args.get("action", "code"))
    # 安全（审查 2026-08-17）：path 过 _check_path 沙盒校验——simulate 会
    # exec_module 执行 .py 顶层代码，未校验路径可绕过沙盒读任意文件执行
    path = str(_check_path(str(args.get("path", ""))))
    if action not in ("ui", "code", "simulate", "clip", "anim3d"):
        raise ValueError("action 可选: ui/code/simulate/clip/anim3d")
    return [_TC(json.dumps(layer_check(action, path), ensure_ascii=False, indent=2))]


def _tool_media_check(args: dict) -> "list[types.TextContent]":
    """剪辑/动画检查（用户 2026-08-17：IDE 对剪辑和动画的提升）。

    action=video（视频容器信息：rx-media Rust 优先 + Python 降级——时长/
          分辨率/帧率/编码/损坏）|timeline（Blender VSE 时间线：素材断链/
          时长越界/帧率混用）|anim（动画检查：.blend 场景 action/关键帧/
          骨骼/蒙皮 via Blender 批处理；.glb animations/skin）|
          render（完整渲染验证：blender -b 批处理渲染，默认全帧——
          用户选定"写完即模拟"的 3D/视频版）。
    path=目标文件（必填）；render 可配 frames（ALL 或 1-10）、engine
    （CYCLES/EEVEE/WORKBENCH）、resolution（>0 覆盖宽）、timeout 秒。
    """
    from media_core import video_info, timeline_check, anim_check, render_sim
    action = str(args.get("action", "video"))
    # 安全：path 过 _check_path（沙盒校验——对齐 fs_read/locate_edit 惯例；
    # 污点流风险由工具层拦截，media_core 内部 open 仅接收已校验路径）
    path = str(_check_path(str(args.get("path", ""))))
    if action == "video":
        return [_TC(json.dumps(video_info(path), ensure_ascii=False, indent=2))]
    if action == "timeline":
        return [_TC(json.dumps(timeline_check(path), ensure_ascii=False, indent=2))]
    if action == "anim":
        return [_TC(json.dumps(anim_check(path), ensure_ascii=False, indent=2))]
    if action == "render":
        try:
            resolution = int(args.get("resolution", 0))
            timeout = int(args.get("timeout", 1800))
        except (TypeError, ValueError):
            raise ValueError("resolution/timeout 须为整数")
        r = render_sim(path, frames=str(args.get("frames", "ALL")),
                       engine=str(args.get("engine", "CYCLES")),
                       resolution=resolution, timeout=timeout)
        return [_TC(json.dumps(r, ensure_ascii=False, indent=2))]
    raise ValueError("action 可选: video/timeline/anim/render")


def _tool_std_check(args: dict) -> "list[types.TextContent]":
    """通用工程标准检查（软件/游戏/前端/UI 通用，AetherStudio 启发）。

    检查：text_placeholder（占位/假数据/套话）、name_conflict（重复定义）、
    ui_hardcode（UI 硬编码颜色/尺寸）、magic_number（裸魔法数字）。
    标准契约：默认按此标准执行；项目有特殊条件时，调用方在提示词中
    提前告知（本工具不臆测），否则按默认标准兼容绝大多数项目。
    """
    p = _check_path(str(args["path"]))
    _t0 = time.perf_counter()
    max_files = int(args.get("max_files", 200))
    if not 1 <= max_files <= 500:
        raise ValueError("max_files 须在 1..500")
    # IDE 增强 173：规则过滤（rules 逗号分隔——只报指定规则，定点排查）
    only = {s.strip() for s in str(args.get("rules", "")).split(",") if s.strip()}
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
    # IDE 增强 173：规则过滤应用（只报指定规则）
    if only:
        result["issues"] = [i for i in result.get("issues", [])
                            if i.get("rule") in only]
        result["summary"]["total"] = len(result["issues"])
        result["severity_counts"] = {}
        for i in result["issues"]:
            _s = i.get("severity", "suggestion").lower()
            _sev_k = "warning" if _s == "warn" else _s
            result["severity_counts"][_sev_k] = result["severity_counts"].get(_sev_k, 0) + 1
    # IDE 增强四十四前序：severity_counts 聚合（与 bug_scan 返回结构一致——
    # AI 消费端统一按 severity_counts 判断报告可信度）
    _sev = {}
    for _i in result.get("issues", []):
        _s = str(_i.get("severity", "info")).lower()
        _sev[_s] = _sev.get(_s, 0) + 1
    result["severity_counts"] = _sev
    # IDE 增强 147：最严重标准问题提示（Critical 优先——secret 泄露等）
    _issues = result.get("issues", [])
    _critical = [i for i in _issues
                 if str(i.get("severity", "")).lower() in ("critical", "error")]
    # IDE 增强 204：问题最多文件（worst_file——对称 ui_check 203）
    _worst_f, _worst_n = "", 0
    if _issues:
        _fc: dict[str, int] = {}
        for i in _issues:
            _k = str(i.get("file", ""))
            _fc[_k] = _fc.get(_k, 0) + 1
        _worst_f, _worst_n = max(_fc.items(), key=lambda kv: kv[1])
    result["worst_file"] = (f"{os.path.basename(_worst_f)}（{_worst_n} 条问题）"
                            if _issues else "")
    # IDE 增强 209：最多问题规则（top_rule——批量修复入口，
    # 按规则批量改一处模板多处复用）
    _rc: dict[str, int] = {}
    for i in _issues:
        _r = str(i.get("rule", "unknown"))
        _rc[_r] = _rc.get(_r, 0) + 1
    if _rc:
        _tr, _tn = max(_rc.items(), key=lambda kv: kv[1])
        result["top_rule"] = f"{_tr}（{_tn} 条）"
    else:
        result["top_rule"] = ""
    # IDE 增强 219：文件级规则分布（file_rules——每个文件的问题规则
    # 摘要，修复计划一眼可见；文件名去路径，防输出膨胀）
    _fr: dict[str, dict[str, int]] = {}
    for i in _issues:
        _f = os.path.basename(str(i.get("file", "")))
        _r = str(i.get("rule", "unknown"))
        _fr.setdefault(_f, {})
        _fr[_f][_r] = _fr[_f].get(_r, 0) + 1
    result["file_rules"] = dict(sorted(_fr.items())[:20])
    if _critical:
        _c = _critical[0]
        result["advice"] = (f"最优先：{os.path.basename(str(_c.get('file', '')))}:"
                            f"{_c.get('line')} [{_c.get('rule')}] "
                            f"{str(_c.get('msg', ''))[:40]}"
                            f"（共 {len(_critical)} 条 Critical/error）")
    elif _issues:
        result["advice"] = "无 Critical——按 Warning/Suggestion 分布排查（看 rule_counts）"
    else:
        result["advice"] = "无标准问题"
    # IDE 增强 185：可用规则列表（过滤参数提示——AI 知道 rules= 可传哪些）
    result["available_rules"] = sorted({
        str(i.get("rule")) for i in _issues
    } | {"text_placeholder", "name_conflict", "ui_hardcode", "magic_number",
         "dead_code", "secret_detection", "swallowed_exception", "rule_deprecated",
         "goto_used", "as_narrowing", "as_precision_loss"})
    # IDE 增强 226：扫描耗时（ms——性能可见，对称 kb 215/semantic 216）
    result["elapsed_ms"] = round((time.perf_counter() - _t0) * 1000, 1)
    return [_TC(json.dumps(result, ensure_ascii=False))]


def _tool_cb_index(args: dict) -> "list[types.TextContent]":
    """代码库索引（认知层）：全库扫描构建/更新持久化索引（文件树+符号+哈希），
    返回变更感知（changed/added/removed）——工具知道代码库全貌和你改了哪。"""
    p = _check_path(str(args["path"]))
    _t0 = time.perf_counter()
    try:
        from cb_index_core import index_repo
    except ImportError:
        _dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, _dir)
        from cb_index_core import index_repo  # noqa: F811
    result = index_repo(str(p))
    # IDE 增强 232：索引耗时（ms——索引构建性能可见收官）
    result["elapsed_ms"] = round((time.perf_counter() - _t0) * 1000, 1)
    # 剥离 files（符号表巨大，输出只需统计与变更；files 仅供 scan_repo 内部使用）
    result.pop("files", None)
    # IDE 增强 195：变更摘要建议（changed/added/removed 一眼可见——
    # AI 知道这次索引更新了什么）
    _ch = len(result.get("changed", []))
    _ad = len(result.get("added", []))
    _rm = len(result.get("removed", []))
    result["advice"] = (f"变更 {_ch} / 新增 {_ad} / 删除 {_rm} 个文件"
                        f"（有变更优先 cb_scan 看 issues）"
                        if (_ch or _ad or _rm) else "无变更——索引已是最新")
    return [_TC(json.dumps(result, ensure_ascii=False))]


def _tool_cb_status(args: dict) -> "list[types.TextContent]":
    """代码库状态（认知层）：读取索引摘要（文件树+符号+上次变更），不重建。"""
    p = _check_path(str(args["path"]))
    _t0 = time.perf_counter()
    try:
        from cb_index_core import repo_status
    except ImportError:
        _dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, _dir)
        from cb_index_core import repo_status  # noqa: F811
    result = repo_status(str(p))
    # IDE 增强 237：查询耗时（ms——性能可见收官）
    if isinstance(result, dict):
        result["elapsed_ms"] = round((time.perf_counter() - _t0) * 1000, 1)
    return [_TC(json.dumps(result, ensure_ascii=False))]


def _tool_cb_scan(args: dict) -> "list[types.TextContent]":
    """全库扫描（认知层）：增量索引 + UI 规则全库扫描，变更优先排序。"""
    p = _check_path(str(args["path"]))
    _t0 = time.perf_counter()
    max_files = int(args.get("max_files", 200))
    if not 1 <= max_files <= 500:
        raise ValueError("max_files 须在 1..500")
    # IDE 增强 175：规则过滤（rules 逗号分隔——对称 std_check 173/bug_scan 174）
    _only = {s.strip() for s in str(args.get("rules", "")).split(",") if s.strip()}
    try:
        from cb_index_core import scan_repo
    except ImportError:
        _dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, _dir)
        from cb_index_core import scan_repo  # noqa: F811
    result = scan_repo(str(p), max_files=max_files)
    # IDE 增强 175：规则过滤应用
    if _only:
        result["issues"] = [i for i in result.get("issues", [])
                            if i.get("rule") in _only]
        result["issue_count"] = len(result["issues"])
    # IDE 增强 187：可用规则列表（rules= 可传哪些——对称 std_check 185/bug_scan 186）
    result["available_rules"] = sorted({
        str(i.get("rule")) for i in result.get("issues", [])
    } | {"ui_root_missing", "camera_missing", "mode_isolation", "focus_pass",
         "font_missing", "z_ordering", "no_interaction"})
    # IDE 增强 282：规则引擎来源标注（AI 知道 no_interaction 覆盖四引擎）
    result["rule_engines"] = {
        "ui_root_missing": "bevy", "camera_missing": "bevy",
        "mode_isolation": "bevy", "focus_pass": "bevy",
        "font_missing": "bevy", "z_ordering": "bevy",
        "no_interaction": "bevy/godot/unity/flutter",
    }
    # IDE 增强 206：问题最多文件（worst_file——扫描四入口收官，
    # 对称 std 204/ui 203/bug 205）
    _is = result.get("issues", [])
    if _is:
        _fc: dict[str, int] = {}
        for i in _is:
            _k = str(i.get("file", ""))
            _fc[_k] = _fc.get(_k, 0) + 1
        _wf, _wn = max(_fc.items(), key=lambda kv: kv[1])
        result["worst_file"] = f"{os.path.basename(_wf)}（{_wn} 条问题）"
    else:
        result["worst_file"] = ""
    # IDE 增强 207：变更文件列表（priority=changed 的 issue 文件去重——
    # 你正在改的文件，优先排查；命名 changed_list 避免与既有
    # changed_files=数量 语义冲突）
    result["changed_list"] = sorted({
        str(i.get("file", "")) for i in _is if i.get("priority") == "changed"
    })
    # IDE 增强 214：最多问题规则（top_rule——扫描四入口收官，
    # 对称 std 209/bug 210/ui 213）
    _rc: dict[str, int] = {}
    for i in _is:
        _r = str(i.get("rule", "unknown"))
        _rc[_r] = _rc.get(_r, 0) + 1
    if _rc:
        _tr, _tn = max(_rc.items(), key=lambda kv: kv[1])
        result["top_rule"] = f"{_tr}（{_tn} 条）"
    else:
        result["top_rule"] = ""
    # IDE 增强 222：文件级规则分布（file_rules——对称 std 219/ui 220/bug 221，
    # 扫描四入口收官）
    _fr: dict[str, dict[str, int]] = {}
    for i in _is:
        _f = os.path.basename(str(i.get("file", "")))
        _r = str(i.get("rule", "unknown"))
        _fr.setdefault(_f, {})
        _fr[_f][_r] = _fr[_f].get(_r, 0) + 1
    result["file_rules"] = dict(sorted(_fr.items())[:20])
    # IDE 增强 229：扫描耗时（ms——扫描全入口收官 std 226/bug 227/ui 228/cb 229）
    result["elapsed_ms"] = round((time.perf_counter() - _t0) * 1000, 1)
    return [_TC(json.dumps(result, ensure_ascii=False))]


def _tool_code_complete(args: dict) -> "list[types.TextContent]":
    """LSP 自动补全（基于真实语法树，不过期不污染）。

    自动读文件（或传 text）→ 按后缀探测语言 → 调 cae_lsp_query(completion)
    → 格式化候选 [{label, kind, detail}]。语言服务器未安装时返回 ok:false+hint，
    不会伪造结果。光标默认最后一行行尾。
    """
    path = str(args.get("path", "") or "")
    _t0 = time.perf_counter()
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
    # IDE 增强 97：候选去重（同 label 保留 kind 优先级最高者——LSP 常返回
    # 重复 label 不同 detail；去重省 token + 候选干净）
    _KIND_RANK = {"Method": 0, "Function": 1, "Class": 2, "Constructor": 3,
                  "Field": 4, "Property": 4, "Variable": 5, "Module": 6,
                  "Interface": 7, "Enum": 8, "Text": 9}
    out = []
    seen: dict[str, int] = {}
    for it in items[:50]:
        label = it.get("label") or it.get("insertText") or ""
        if not label:
            continue
        kind = _LSP_KIND_NAMES.get(it.get("kind"), it.get("kind"))
        detail = str(it.get("detail") or "")[:80]
        key = str(label)[:160]
        rank = _KIND_RANK.get(str(kind), 9)
        prev = seen.get(key)
        if prev is not None and prev <= rank:
            continue  # 已保留更优候选
        seen[key] = rank
        out.append({"label": key, "kind": kind, "detail": detail})
    # 阶段3 引擎语义层：LSP 空结果时附 game_api 词典提示（游戏文件——
    # .gd → godot、.rs 含 bevy 导入 → bevy；未收录诚实提示防幻觉）
    game_hints: list = []
    if not out:
        try:
            from game_api import BEVY_API, GODOT_API
            engine = None
            if p.suffix.lower() == ".gd":
                engine = "godot"
            elif p.suffix.lower() == ".rs" and "bevy" in text:
                engine = "bevy"
            if engine:
                db = BEVY_API if engine == "bevy" else GODOT_API
                # 光标前最后一个词 → 词典前缀/包含匹配（防幻觉：仅提示已收录）
                m = re.search(r"([A-Za-z_]\w*)\s*$",
                              text[: character if character >= 0 else len(text)])
                prefix = m.group(1) if m else ""
                if prefix:
                    for k, (kind, desc) in db.items():
                        if prefix.lower() in k.lower() or k.lower() in prefix.lower():
                            game_hints.append({"symbol": k, "kind": kind,
                                               "description": desc[:60]})
                            if len(game_hints) >= 6:
                                break
        except Exception:
            game_hints = []
    return [_tr(True, f"completion {len(out)} 项（去重 {len(items) - len(out)}）",
                {"language": language_id,
                 "position": {"line": line, "character": character},
                 "items": out,
                 "game_hints": game_hints,
                 # IDE 增强 247：补全耗时（ms——收官）
                 "elapsed_ms": round((time.perf_counter() - _t0) * 1000, 1)})]


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
    """报错文本 → file:line 精准定位（含上下文片段，走沙盒校验）。

    核心合并（2026-08-16）：action=bisect 时走 git bisect 式二分定位
    （原独立工具 bug_bisect 并入——只读计划，execute=true 需 L4 授权）。
    """
    action = str(args.get("action", "locate")).strip()
    if action == "bisect":
        from causal_debug import bug_bisect
        root = _check_path(str(args.get("root", "")))
        good = str(args.get("good_commit", ""))
        bad = str(args.get("bad_commit", "")) or "HEAD"
        cmd = str(args.get("test_cmd", "cargo test"))
        execute = bool(args.get("execute", False))
        return [_TC(json.dumps(bug_bisect(str(root), good, bad, cmd,
                                  execute=execute),
                               ensure_ascii=False, indent=2))]
    if action != "locate":
        raise ValueError("未知 action: %s（可选 locate/bisect）" % action)
    text = str(args["error_text"])
    _t0 = time.perf_counter()
    # IDE 增强 160：上下文行数可调（默认 3——大报错上下文按需放宽）
    ctx = int(args.get("context_lines", _BUG_CONTEXT))
    if not 0 <= ctx <= 50:
        raise ValueError("context_lines 须在 0..50")
    matches = []
    for m in _TRACEBACK_RE.finditer(text):
        matches.append((m.group(1), int(m.group(2)), 0, (m.group(3) or "").strip()))
    if not matches:
        for m in _SIMPLE_POS_RE.finditer(text):
            raw_p = m.group(1)
            # IDE 增强 273：file:/// URI 前缀清洗（Dart/JS 报错 file:///u/x.dart:7
            # 的 e: 盘符误判——正则把 file 末字符+:// 吃成盘符）
            if len(raw_p) >= 4 and raw_p[0].isalpha() and raw_p[1:4] == "://":
                raw_p = raw_p.split("://", 1)[1].lstrip("/")
            matches.append((raw_p, int(m.group(2)), int(m.group(3) or 0), ""))
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
        start, end = max(1, line - ctx), min(len(src_lines), line + ctx)
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
        # IDE 增强 130：定位后接建议（修复入口直达——定位→修复闭环）
        "advice": ("用 `ide_actions` 看修复建议 / `ide_quest action=auto` 自动诊断链"
                   if locations else "未匹配到 file:line——可用 locate_edit 自然语言定位"),
        # IDE 增强 248：定位耗时（ms——收官）
        "elapsed_ms": round((time.perf_counter() - _t0) * 1000, 1),
    }, ensure_ascii=False))]




def _tool_scan_all(args: dict) -> "list[types.TextContent]":
    """自研高并发插件：五路任务级并行扫描（bug_scan+std_check+ui_check+cb_scan+cross_taint）
    全项目扫描——写完即挖的全量版（比 vuln_scan 多 cb_scan 代码库索引 + cross_taint 污点）。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import os as _os
    path = args.get("path") or _os.getcwd()
    max_files = int(args.get("max_files", 100))
    _t0 = time.perf_counter()
    results = {"path": path, "lanes": {}}

    def run_one(name: str, fn) -> None:
        try:
            r = fn({"path": path, "max_files": max_files})
            t = r[0].text if isinstance(r, list) else str(r)
            results["lanes"][name] = json.loads(t) if t.startswith("{") else {"raw": t[:200]}
        except Exception as e:  # noqa: BLE001
            results["lanes"][name] = {"error": str(e)}

    lanes = [
        ("bug_scan", _tool_bug_scan),
        ("std_check", _tool_std_check),
        ("ui_check", _tool_ui_check),
        ("cb_scan", _tool_cb_scan),
        ("cov_scan", _tool_cov_scan),
    ]
    with _CONCURRENCY_SEM:
        pool = _pool()
        futs = [pool.submit(run_one, n, f) for n, f in lanes]
        for fut in as_completed(futs):
            fut.result()
    results["elapsed_ms"] = int((time.perf_counter() - _t0) * 1000)
    results["ok"] = not any("error" in v for v in results["lanes"].values())
    return [types.TextContent(type="text", text=json.dumps(results, ensure_ascii=False))]


def _tool_train_export(args: dict) -> "list[types.TextContent]":

    """可微分编程：从修复提交自动提取训练样本（diff 模式 → 检测规则/学习样本）。
    每个修复提交生成 {commit, file, diff, pattern} 样本落盘 train_data/samples.jsonl——
    代码可被学习训练（后续模型微调/规则生成直接用）。"""
    import os as _os
    import subprocess as _sp
    path = args.get("path") or _os.getcwd()
    count = int(args.get("count", 20))
    out_dir = args.get("out_dir") or _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "train_data")
    _os.makedirs(out_dir, exist_ok=True)
    sample_path = _os.path.join(out_dir, "samples.jsonl")
    try:
        # 最近 N 个提交（--name-only 直接给文件列表——可靠）
        r = _sp.run(["git", "-C", path, "log", "-n", str(count), "--name-only", "--format=%h"],
                    capture_output=True, text=True, timeout=60,
                    encoding="utf-8", errors="replace")
        commits = []
        cur = None
        for ln in r.stdout.splitlines():
            ln = ln.strip()
            if ln and len(ln) <= 12 and not ln.startswith((".", "/")) and "/" not in ln and "." not in ln and commits is not None:
                # 提交哈希行（短哈希——无点无斜杠）
                if cur:
                    commits.append(cur)
                cur = {"sha": ln, "files": []}
            elif cur is not None and ln:
                if ln.endswith((".rs", ".py", ".ts", ".js", ".go", ".toml", ".mjs")):
                    cur["files"].append(ln)
        if cur:
            commits.append(cur)
        samples = []
        for c in commits:
            for f in c["files"][:5]:
                # 每个修复文件的 diff（前 40 行——模式提取）
                d = _sp.run(["git", "-C", path, "show", f"{c['sha']}--{f}"],
                            capture_output=True, text=True, timeout=60,
                            encoding="utf-8", errors="replace")
                diff = d.stdout[:1200]
                if not diff:
                    d = _sp.run(["git", "-C", path, "show", c["sha"], "--", f],
                                capture_output=True, text=True, timeout=60,
                                encoding="utf-8", errors="replace")
                    diff = d.stdout[:1200]
                # 简单模式提取：删除行（bug 模式）vs 新增行（修复模式）
                removed = [ln[1:].strip()[:80] for ln in diff.splitlines()
                           if ln.startswith("-") and not ln.startswith("---")]
                added = [ln[1:].strip()[:80] for ln in diff.splitlines()
                         if ln.startswith("+") and not ln.startswith("+++")]
                if removed or added:
                    samples.append({
                        "commit": c["sha"],
                        "file": f,
                        "bug_patterns": removed[:5],
                        "fix_patterns": added[:5],
                        "diff_excerpt": diff[:800],
                    })
        with open(sample_path, "a", encoding="utf-8") as fp:
            for smp in samples:
                fp.write(json.dumps(smp, ensure_ascii=False) + chr(10))
        return [types.TextContent(type="text", text=json.dumps({
            "ok": True, "samples": len(samples), "path": sample_path,
            "note": "追加写入 samples.jsonl（每提交=一个学习样本）"}, ensure_ascii=False))]
    except Exception as e:  # noqa: BLE001
        return [types.TextContent(type="text", text=json.dumps(
            {"ok": False, "error": str(e)}, ensure_ascii=False))]


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
                except (ValueError, TypeError):  # 尽力而为
                    pass
            return [card]
        summary = f"{name}: {text[:200]}{'…' if len(text) > 200 else ''}"
        card = _tr(True, summary, _truncate_detail(parsed, max_detail))
        if exp_id and isinstance(card.text, str):
            try:
                d0 = json.loads(card.text)
                d0["experience_id"] = exp_id
                card.text = json.dumps(d0, ensure_ascii=False)
            except (ValueError, TypeError):  # 尽力而为
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
    "ide_health": (_tool_ide_health, _schema({}, []), "IDE 工具族健康自检（graph_index/LSP server/缓存/工具完整性——诊断 IDE 弱在哪）"),
    "layer_check": (_tool_layer_check, _schema({"action": _S("string", "ui/code/simulate/clip/anim3d（默认 code）"), "path": _S("string", "目标文件（必填）")}, ["path"]), "分层开发理念（UI 布局→动画→美术 / 代码 骨架→逻辑→优化 / 剪辑 粗剪→精剪→调色音效 / 3D动画 建模绑定→K帧→渲染，含顺序违规校验）+ 写完即模拟"),
    "media_check": (_tool_media_check, _schema({"action": _S("string", "video/timeline/anim/render（默认 video）"), "path": _S("string", "目标文件（视频/.blend/.glb）"), "frames": _S("string", "render 用：ALL 或 1-10"), "engine": _S("string", "render 用：CYCLES/EEVEE/WORKBENCH"), "resolution": _S("integer", "render 用：>0 覆盖宽"), "timeout": _S("integer", "render 用：超时秒(默认1800)")}, ["path"]), "剪辑/动画检查（视频容器 rx-media Rust+Python 降级 / Blender VSE 时间线断链 / .blend+.glb 动画完整性 / 完整渲染验证）"),
    "ide_complete": (_tool_ide_complete, _schema({
        "root": _S("string", "代码库根目录"),
        "file": _S("string", "当前文件"),
        "prefix": _S("string", "补全前缀"),
    }, ["root", "prefix"]), "符号补全（tree-sitter 图降级版，无 LSP 可用；当前文件优先）"),
    "ide_references": (_tool_ide_references, _schema({
        "root": _S("string", "代码库根目录"),
        "symbol": _S("string", "要查的符号"),
    }, ["root", "symbol"]), "查找符号定义与全部引用（IDE goto-references，无 LSP 可用）"),
    "game_check": (_tool_game_check, _schema({
        "path": _S("string", "项目路径"),
        "rules": _S("string", "可选：逗号分隔规则名过滤（frame_io/input_unthrottled/physics_scale/frame_rate_dependent）"),
    }, ["path"]), "游戏工程引擎中立检查（skill M1/M5：每帧 IO 红线/输入节流不变量/物理参数数量级）"),
    "game_feel": (_tool_game_feel, _schema({
        "path": _S("string", "项目路径（或单文件）"),
    }, ["path"]), "表现寄存器判定（skill M2：character/abstract/serious——效果建议前先定寄存器）"),
    "game_api": (_tool_game_api, _schema({
        "engine": _S("string", "引擎（bevy/godot）"),
        "symbol": _S("string", "API 符号名（Transform/_process/Button…）"),
    }, ["engine", "symbol"]), "引擎 API 语义查询（Bevy 0.18 优先+Godot 4 基础——未收录诚实拒绝防幻觉）"),
    "game_verify": (_tool_game_verify, _schema({
        "path": _S("string", "项目路径"),
    }, ["path"]), "可复现验证检查（skill M4：smoke 脚本/XDG/日志捕获——无头验证不靠猜）"),
    "game_rules": (_tool_game_rules, _schema({
        "path": _S("string", "项目路径"),
        "action": _S("string", "load（默认）/save"),
        "rules": _S("object", "save 时的规则对象（engine/physics_range/…）"),
    }, ["path"]), "项目级游戏规则读写（通用默认 + 项目覆盖——在游戏文件里再搞一个）"),
    "watch_status": (_tool_watch_status, _schema({}, []),
                     "实时监听状态（阶段1：文件改动监听线程/间隔/根/跟踪数）"),
    "predict_impact": (_tool_predict_impact, _schema({
        "root": _S("string", "代码库根目录"),
        "symbol": _S("string", "要改的符号名"),
        "file_hint": _S("string", "可选：限定文件（改动所在文件）"),
    }, ["root", "symbol"]), "预知引擎（阶段2：改前预测影响面+教训+规则——改后跑 ide_fusion 确认）"),
    "speculate": (_tool_speculate, _schema({
        "current_file": _S("string", "当前编辑文件"),
        "recent_tools": _S("array", "可选：最近调用工具列表"),
        "recent_paths": _S("array", "可选：最近路径列表"),
    }, []), "推测执行（阶段3：预测下一步→预执行白名单只读→缓存秒回——安全边界：仅幂等只读）"),
    "causal_trace": (_tool_causal_trace, _schema({
        "root": _S("string", "项目路径"),
        "fail_keyword": _S("string", "失败关键词（fail/error/崩溃…）"),
    }, ["root"]), "因果建模（为什么错：失败事件→回溯因果链——git 提交+工具调用——溯源到行为）"),
    "bug_bisect": (_tool_bug_bisect, _schema({
        "root": _S("string", "项目路径"),
        "good_commit": _S("string", "已知正常提交（hash）"),
        "bad_commit": _S("string", "已知坏提交（hash——默认 HEAD）"),
        "test_cmd": _S("string", "判定命令（如 cargo test）"),
        "execute": _S("boolean", "true=实际执行 git bisect（L4 授权——需 __authorized:true；会 checkout 工作区）"),
    }, ["root", "good_commit"]), "git bisect 式二分定位（默认只读计划；execute=true 实际执行——L4 授权需显式确认）"),
    "causal_link": (_tool_causal_link, _schema({
        "root": _S("string", "项目路径"),
        "effect": _S("string", "结果（失败/现象）"),
        "cause": _S("string", "原因（行为/变更）"),
    }, ["root", "effect", "cause"]), "记录因果链（cause→effect 入 scan-log——行为链回放溯源）"),
    "optimize_code": (_tool_optimize_code, _schema({
        "path": _S("string", "文件路径"),
        "perf_goal": _S("string", "性能目标（响应时间<10ms/内存…）"),
    }, ["path"]), "可微分编程落地（性能目标驱动优化器：复杂度热点+等价重写建议——规则驱动，真梯度下降为未来方向）"),
    "code_embed": (_tool_code_embed, _schema({
        "path": _S("string", "文件路径"),
        "compare": _S("string", "可选：对比文件路径（相似函数检索）"),
    }, ["path"]), "AST 符号嵌入（函数特征向量——相似函数检索；真·语义嵌入可替换 mini_bert）"),
    "telemetry_status": (_tool_telemetry_status, _schema({
        "since_ts": _S("number", "可选：只看该时间戳之后的调用"),
    }, []), "遥测状态快照（AI 可读：工具耗时 TOP/错误率/调用量 + daemon 心跳表——卡死/热点一眼看穿）"),
    "telemetry_snapshot": (_tool_telemetry_snapshot, _schema({}, []),
                           "SGG PerfMeter 式一键体检包：卡死检测 + 聚合 + 慢工具 + 最近错误 + 告警——AI 读一份知系统全貌"),
    "alarm_check": (_tool_alarm_check, _schema({
        "thresholds": _S("object", "可选阈值覆盖：p95_slow_ms/err_rate_high/stale_sec"),
    }, []), "告警规则引擎（自动监控：P95 慢/错误率超限/daemon 卡死/总错误率→alarms.jsonl，30 分钟去重）"),
    "failure_analyze": (_tool_failure_analyze, _schema({
        "text": _S("string", "traceback/失败文本"),
        "root": _S("string", "可选：项目路径（关联 git 提交/scan-log）"),
        "limit": _S("integer", "可选：关联条数上限（默认 200）"),
    }, ["text"]), "根因分析（RCA：traceback→根因链报告——关联遥测/scan-log/git/告警，候选按证据强度排序）"),
    "cov_scan": (_tool_cov_scan, _schema({
        "path": _S("string", "项目路径"),
        "mode": _S("string", "static（AST 死代码，默认）/ dynamic（coverage.py 实测）/ auto"),
        "limit": _S("integer", "可选：文件数上限（默认 2000）"),
    }, ["path"]), "代码覆盖率/死代码分析（定位从未执行的代码=隐形炸弹：未引用符号清单 + 未用 import；dynamic 实测未覆盖 TOP）"),
    "stress_scan": (_tool_stress_scan, _schema({
        "path": _S("string", "可选：项目路径（index/file 场景用）"),
        "mode": _S("string", "auto（默认）/ log / telemetry / index / file"),
        "scale": _S("integer", "可选：条数规模（默认 10 万，上限 100 万）"),
        "timeout": _S("integer", "可选：总时限秒（默认 300）"),
    }, []), "压力测试（工具集自身：scan-log/遥测 8 线程并发 append 丢数据检测 + 大仓库遍历/大文件读取计时）"),
    "replay_record": (_tool_replay_record, _schema({
        "name": _S("string", "录制名（字母数字-_，≤64）"),
        "step": _S("object", "步骤：{type: tool, tool, args} 或 {type: cmd, cmd, cwd}"),
    }, ["name", "step"]), "录制一步操作（BugCraft 式：崩溃复现序列第一步）"),
    "replay_run": (_tool_replay_run, _schema({
        "name": _S("string", "录制名（空=列出所有录制）"),
        "stop_on_fail": _S("boolean", "可选：失败即停（默认 true）"),
    }, ["name"]), "重放录制序列（偶现变必现——failed_at 即复现点；cmd 步骤需 __authorized）"),
    "sage_scan": (_tool_sage_scan, _schema({
        "root": _S("string", "仓库路径"),
        "commits": _S("integer", "可选：最近 N 个提交（默认 1）"),
        "since": _S("string", "可选：--since 时间范围（优先于 commits）"),
    }, ["root"]), "SAGE 式语义回归优先级（commit 变更+语义标签→优先测试清单——海量内容锁定风险区）"),
    "code_search": (_tool_code_search, _schema({
        "query": _S("string", "自然语言/中文/符号查询（如：放置模块时命中盒计算的函数）"),
        "root": _S("string", "可选：项目根（缺省用当前索引）"),
        "k": _S("integer", "可选：返回条数（默认 20，上限 50）"),
    }, ["query"]), "本地语义代码检索（Rust BM25+符号加权：中文/英文/标识符 → 文件:行）——explore_code 关键词失败自动兜底"),
    "telemetry_query": (_tool_telemetry_query, _schema({
        "limit": _S("integer", "最近 N 条（默认 20，上限 200）"),
        "tool": _S("string", "可选：按工具名过滤"),
        "status": _S("string", "可选：ok/error 过滤"),
    }, []), "遥测记录查询（流式读尾部——工具耗时/错误/心跳原始记录）"),
    "mesh_check": (_tool_mesh_check, _schema({
        "path": _S("string", "网格文件（.obj/.stl/.ply）"),
    }, ["path"]), "网格拓扑健康报告（TetSphere 概念：非流形/破面/孤立顶点——引擎即用检测）"),
    "mesh_optimize": (_tool_mesh_optimize, _schema({
        "path": _S("string", "网格文件"),
        "target_ratio": _S("number", "目标精简率（默认 0.5）"),
    }, ["path"]), "网格精简建议（NURBS 概念：welding+共面合并——表示效率）"),
    "mesh_splat": (_tool_mesh_splat, _schema({
        "path": _S("string", "网格文件"),
    }, ["path"]), "三角面片→可训练参数表（Triangle Splatting 概念：顶点/法线/面索引张量——真梯度优化未来方向）"),
    "voxelize": (_tool_voxelize, _schema({
        "path": _S("string", "网格文件"),
        "resolution": _S("integer", "体素分辨率（4..128，默认 16）"),
    }, ["path"]), "网格体素化（Radiant Foam 概念：体素占用表示——光线追踪/碰撞基础）"),
    "geometry_exchange": (_tool_geometry_exchange, _schema({
        "path": _S("string", "源网格文件"),
        "target_format": _S("string", "目标格式（obj/stl/ply）"),
    }, ["path", "target_format"]), "格式间直接几何交换（Rhino.Inside 概念：无中间文件——内容直接输出可写文件）"),
    "half_edge": (_tool_half_edge, _schema({
        "path": _S("string", "网格文件"),
    }, ["path"]), "半边数据结构分析（Manifold3D 概念：邻接/边界/流形/1-ring——高速拓扑操控）"),
    "mesh_union": (_tool_mesh_union, _schema({
        "paths": _S("array", "网格文件列表（1..10 个）"),
    }, ["paths"]), "网格并集合并（PicoGK 概念：顶点焊接——紧凑几何操作；真 CSG 标注未来方向）"),
    "mesh_clip": (_tool_mesh_clip, _schema({
        "path": _S("string", "网格文件"),
        "plane": _S("array", "裁剪平面 [a,b,c,d]（ax+by+cz+d=0）"),
        "keep": _S("string", "保留侧（keep_positive 默认/keep_negative）"),
    }, ["path", "plane"]), "平面裁剪（真·CSG 基础：差集操作——half-edge 顶点分裂）"),
    "geom_graph": (_tool_geom_graph, _schema({
        "nodes": _S("array", "节点图（[{id,type,args}]——load/union/clip/exchange/voxelize）"),
        "outputs": _S("array", "输出节点 id 列表"),
    }, ["nodes"]), "几何节点图执行（Grasshopper 概念：节点即操作——可视化编程 DSL 零依赖版）"),
    "geom_example": (_tool_geom_example, _schema({
        "kind": _S("string", "示例类型（union/clip/graph）"),
    }, []), "可运行几何示例生成（PicoGK Program.cs 概念：VS Code 直接运行——零依赖）"),
    "patch_learn": (_tool_patch_learn, _schema({
        "diff": _S("string", "标准统一 diff（- 行=修复前漏洞代码）"),
        "language": _S("string", "目标语言（.py/.js 等，默认 .py）"),
    }, ["diff"]), "补丁学规则（KNighter 概念：从修复 diff 提取模式→生成检测规则——可直接加入 vuln_rules.json）"),
    "half_edge_adjacency": (_tool_half_edge_adjacency, _schema({
        "path": _S("string", "网格文件"),
        "vertex": _S("integer", "顶点索引"),
    }, ["path", "vertex"]), "半边邻接查询（Manifold3D 概念升级：1-ring/关联面/边界——拓扑操控接口）"),
    "mesh_boolean": (_tool_mesh_boolean, _schema({
        "paths": _S("array", "2 个网格文件"),
        "op": _S("string", "操作（intersect 默认/union/subtract）"),
    }, ["paths"]), "CSG 布尔检测层（AABB 相交/包含/分离 + 面心采样标记——真·CSG 前置）"),
    "voxel_surface": (_tool_voxel_surface, _schema({
        "path": _S("string", "网格文件"),
        "resolution": _S("integer", "体素分辨率（4..128，默认 16）"),
    }, ["path"]), "表面体素提取（Radiant Foam 概念升级：表面点云——碰撞/光线追踪可用）"),
    "runtime_state": (_tool_runtime_state, _schema({
        "path": _S("string", "项目路径"),
        "source": _S("string", "状态来源（bevy_brp/file/scan——BRP 不可用时降级）"),
        "state": _S("object", "可选：直接上报的状态（file 来源）"),
    }, ["path"]), "运行状态回喂（阶段4：BRP localhost:15702 实体状态/文件状态 → scan-log runtime_state——双向反馈）"),
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
    "repo_health": (_tool_repo_health, _schema({"action": _S("string", "dedup/incomplete/branch/conflict/all"), "root": _S("string", "项目根（默认 UNIFIED_RX_PROJECT 或 cwd）"), "top": _S("integer", "结果上限(默认20)")}, ["action"]), "代码库健康四理念（去重/剔残缺/分支/标矛盾——用户主要目标理念；只读检测 + 健康评分）"),
    "cost_report": (_tool_cost_report, _schema({"action": _S("string", "summary/estimate/code（默认 summary）"), "model": _S("string", "模型单价键（deepseek-chat 默认）"), "text": _S("string", "estimate 用：待估算文本"), "path": _S("string", "code 用：代码文件/目录")}, []), "成本核算（调用次数+token+成本：按工具/天/项目汇总，或估算文本/代码成本——用户要求每个代码和工具调用都算成本）"),
    "chatlog_search": (_tool_chatlog_search, _schema({"action": _S("string", "search/collect/status（默认 search）"), "query": _S("string", "关键词（匹配 title+text）"), "agent": _S("string", "智能体过滤（marvis/hermes/trae/qoder）"), "limit": _S("integer", "结果上限(默认20)"), "since_days": _S("integer", "只看最近 N 天"), "agents": _S("string", "collect 用：逗号分隔智能体列表")}, []), "不同智能体聊天记录检索（Marvis/Hermes 聊天记忆 + Trae/Qoder 编辑留痕——统一索引去重）"),
    "local_tools": (_tool_local_tools, _schema({"action": _S("string", "scan/discover/run（默认 discover）"), "query": _S("string", "discover 用：名称过滤"), "category": _S("string", "discover 用：目录过滤"), "name": _S("string", "run 用：已注册工具名"), "args": _S("array", "run 用：参数列表"), "timeout": _S("integer", "run 用：超时秒(默认60)")}, []), "本地工具注册表与安全调用桥（D:\\rj 下 639 个工具：7zip/Blender/Everything/aria2 等——白名单+危险参数黑名单）"),
        "scan_now": (_tool_scan_now, _schema({"path": _S("string", "扫描路径（默认 cwd/git 根）"), "max_files": _S("integer", "文件上限(默认100)")}, []), "常态扫描（写完即挖）：vuln_scan + bug_scan 一次跑——每次代码完成后立刻调用，不等到收尾"),
    "scan_delta": (_tool_scan_delta, _schema({"path": _S("string", "git 仓库路径（默认 cwd）")}, []), "增量扫描：git diff 变更文件只扫变更（快——写完一个文件立刻挖）"),
    "scan_all": (_tool_scan_all, _schema({"path": _S("string", "扫描路径（默认 cwd）"), "max_files": _S("integer", "文件上限(默认100)")}, []), "自研高并发插件：五路任务级并行（bug_scan+std_check+ui_check+cb_scan+cov_scan）——全量常态扫描"),
    "train_export": (_tool_train_export, _schema({"path": _S("string", "git 仓库路径（默认 cwd）"), "count": _S("integer", "最近提交数(默认20)"), "out_dir": _S("string", "样本输出目录（默认 train_data/）")}, []), "可微分编程：修复提交自动提取训练样本（diff bug/fix 模式 → samples.jsonl——代码可学习训练）"),
    "git_bisect_find": (_tool_git_bisect_find, _schema({"path": _S("string", "git 仓库路径（默认 cwd）"), "good": _S("string", "已知无 bug 的提交（必填）"), "bad": _S("string", "已知坏提交（默认 HEAD）"), "test_cmd": _S("string", "判定命令（0=好非0=坏，默认 cargo test）")}, ["good"]), "可回溯：git bisect 自动二分定位引入 bug 的提交（自动化测试驱动）"),
    "backup": (_tool_backup, _schema({"action": _S("string", "backup/list/rollback（默认 list）"), "root": _S("string", "项目根（必填）"), "keep": _S("integer", "保留快照份数(默认7，删最旧)"), "date": _S("string", "rollback 用：YYYYMMDD 快照日期")}, ["root"]), "每日备份与回溯（git commit+tag + 限量快照 zip 7 份——备份不会太多；rollback 恢复前自动另存当前状态）"),
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
    "net_chaos": (_tool_net_chaos, _schema({
        "action": _S("string", "start（默认自动分配端口）/stop/status（默认）/sanity"),
        "listen": _S("string", "start/stop 时：代理监听地址（start 留空=自动分配空闲端口）"),
        "target": _S("string", "start 时：目标地址 host:port（默认 127.0.0.1:80）"),
        "delay": _S("number", "start/sanity 时：每块延迟毫秒（默认 0）"),
        "loss": _S("number", "丢包概率 0-100（默认 0）"),
        "reorder": _S("number", "乱序概率 0-100（默认 0）"),
        "bandwidth": _S("integer", "带宽上限 KB/s（默认 0=不限）"),
    }, []), "弱网模拟（rx-net Clumsy 式本地 TCP 代理：延迟/丢包/乱序/限速注入——测网络鲁棒性；进程启停管理）"),
}

# ─────────────────────────────────────────────────────────────
# 核心合并（2026-08-16，用户要求"把核心合并，unified-rx 一堆杂物"）：
# 保守方案——13 组同域族 → 组合工具（97→73，能力零丢失，action 分发）。
# 旧工具函数保留为内部实现（_tool_* 未改名），仅注册表层合并；
# 旧工具名不再暴露（先例：2026-08-11 去重 29 单工具 → 6 组合）。
# 注册表文本中的旧条目（mesh_check 等）在 _MERGED 循环中被 del，不参与定义。
# ─────────────────────────────────────────────────────────────

def _combo_tool(action_map: dict):
    """组合工具工厂：action 分发 → 旧函数（参数透传，action 字段剥离）。"""
    def _dispatch(args: dict):
        action = str(args.get("action", "")).strip()
        fn = action_map.get(action)
        if fn is None:
            raise ValueError("未知 action: %s（可选 %s）" % (action, sorted(action_map)))
        sub = dict(args)
        sub.pop("action", None)
        return fn(sub)
    _dispatch.__name__ = "combo"
    return _dispatch


_MERGED = {
    "mesh": {"boolean": _tool_mesh_boolean, "check": _tool_mesh_check,
             "clip": _tool_mesh_clip, "optimize": _tool_mesh_optimize,
             "splat": _tool_mesh_splat, "union": _tool_mesh_union},
    "telemetry": {"query": _tool_telemetry_query, "snapshot": _tool_telemetry_snapshot,
                  "status": _tool_telemetry_status},
    "replay": {"record": _tool_replay_record, "run": _tool_replay_run},
    "causal": {"link": _tool_causal_link, "trace": _tool_causal_trace},
    "half_edge": {"analyze": _tool_half_edge, "adjacency": _tool_half_edge_adjacency},
    "repo": {"graph": _tool_repo_graph, "wiki": _tool_repo_wiki},
    "agent": {"orchestrate": _tool_agent_orchestrate, "roles": _tool_agent_roles},
    "geom": {"example": _tool_geom_example, "graph": _tool_geom_graph},
    "voxel": {"surface": _tool_voxel_surface, "voxelize": _tool_voxelize},
    "scan": {"log": _tool_scan_log, "trend": _tool_scan_trend},
    "game": {"api": _tool_game_api, "check": _tool_game_check, "feel": _tool_game_feel,
             "rules": _tool_game_rules, "verify": _tool_game_verify},
    "lesson": {"extract": _tool_lesson_extract, "feedback": _tool_lesson_feedback,
               "learn": _tool_lesson_learn, "recall": _tool_lesson_recall_lse,
               "rule_feedback": _tool_rule_feedback},
}

# 新工具 schema/描述（action 枚举 + 透传参数提示）
_MERGED_SCHEMAS = {
    "mesh": ({"action": _S("string", "boolean/check/clip/optimize/splat/union"), "path": _S("string", "网格文件"), "paths": _S("array", "boolean/union：网格文件列表"), "plane": _S("array", "clip：裁剪平面 [a,b,c,d]"), "keep": _S("string", "clip：保留侧"), "resolution": _S("integer", "可选 4..128"), "target_ratio": _S("number", "optimize：目标精简率")}, ["action"]),
    "telemetry": ({"action": _S("string", "query/snapshot/status"), "limit": _S("integer", "query：条数"), "tool": _S("string", "query：按工具过滤"), "status": _S("string", "query：ok/error 过滤")}, ["action"]),
    "replay": ({"action": _S("string", "record/run"), "name": _S("string", "录制名"), "step": _S("object", "record：步骤"), "stop_on_fail": _S("boolean", "run：失败即停")}, ["action", "name"]),
    "causal": ({"action": _S("string", "link/trace"), "path": _S("string", "项目路径"), "error_text": _S("string", "trace：报错文本")}, ["action"]),
    "half_edge": ({"action": _S("string", "analyze/adjacency"), "path": _S("string", "网格文件"), "vertex": _S("integer", "adjacency：顶点索引")}, ["action", "path"]),
    "repo": ({"action": _S("string", "graph/wiki"), "root": _S("string", "仓库路径"), "depth": _S("integer", "graph：深度")}, ["action", "root"]),
    "agent": ({"action": _S("string", "orchestrate/roles"), "task": _S("string", "orchestrate：任务"), "repo": _S("string", "orchestrate：仓库")}, ["action"]),
    "geom": ({"action": _S("string", "example/graph"), "kind": _S("string", "example：union/clip/graph"), "nodes": _S("array", "graph：节点图"), "outputs": _S("array", "graph：输出节点")}, ["action"]),
    "voxel": ({"action": _S("string", "surface/voxelize"), "path": _S("string", "网格文件"), "resolution": _S("integer", "可选 4..128")}, ["action", "path"]),
    "scan": ({"action": _S("string", "log/trend"), "root": _S("string", "log：按项目过滤"), "limit": _S("integer", "log：条数"), "window_days": _S("integer", "trend：窗口天数")}, ["action"]),
    "game": ({"action": _S("string", "api/check/feel/rules/verify"), "path": _S("string", "游戏文件/目录")}, ["action"]),
    "lesson": ({"action": _S("string", "recall/feedback/learn/extract/rule_feedback"), "task_description": _S("string", "recall：任务描述"), "lesson_id": _S("string", "feedback：教训 ID"), "delta": _S("number", "feedback：效用增量"), "text": _S("string", "extract：源文本"), "tier": _S("string", "extract：core/work/archive"), "rule": _S("string", "rule_feedback：规则名"), "adopted": _S("boolean", "rule_feedback：采纳")}, ["action"]),
}

for _name, _actions in _MERGED.items():
    _sc, _desc = _MERGED_SCHEMAS[_name]
    _TOOLS[_name] = (_combo_tool(_actions), _schema(_sc, _sc.get("required", [])), _desc)
    for _old_fn in _actions.values():
        for _k in [k for k, v in _TOOLS.items() if v[0] is _old_fn]:
            del _TOOLS[_k]
# bug_bisect 并入 bug_locate（action=bisect）
for _k in [k for k, v in _TOOLS.items() if v[0] is _tool_bug_bisect]:
    del _TOOLS[_k]


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
    # 当前仓库布局：仓库根/server.py + 扩展在 仓库根/vendor/extensions/
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor", "extensions"),
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
# 并发安全（2026-08-14 高并发压力测试）：多线程首次并发加载同一扩展——
# 无锁时双重 exec_module（重复注册/浪费）——检查-加载-写入原子段。
_EXT_LOAD_LOCK = threading.Lock()


def _load_ext(label: str) -> object | None:
    if label in _EXT_LOADED:
        return _EXT_LOADED[label]
    path = os.path.join(_EXT_BASE, label, "server.py")
    try:
        spec = _ilu.spec_from_file_location(f"unifiedrx_{label}", path)
        if spec is None or spec.loader is None:
            return None
        with _EXT_LOAD_LOCK:
            if label in _EXT_LOADED:  # 双检：等待锁期间可能已被加载
                return _EXT_LOADED[label]
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
        cio = _load_ext("ci-optimization")
        if cio is not None and hasattr(cio, "_tool_definitions"):
            for t in cio._tool_definitions():
                # ciopt_ 前缀（合并 2026-08-16：vendored 独立化，跨机可移植）
                _EXT_DEFS[t.name] = ("ci-optimization", "pure", t)
    except Exception as exc:
        print(f"[unified-rx] WARNING: ci-optimization 扩展定义构建失败: {exc}", file=sys.stderr)
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
    tel_ok = True
    tel_err = ""
    _out_text = ""  # 成本核算：输出 token 估算（结果文本，各返回点赋值）
    try:
        import speculate  # 阶段3 推测执行（延迟 import 防启动开销）
        # 阶段3（2026-08-15）：推测执行消费——白名单只读工具命中推测缓存
        # → 秒回（不重复执行）；未命中走正常执行
        if name in speculate.SPECULATE_WHITELIST:
            cached = speculate.consume_speculated(name, arguments or {})
            if cached is not None:
                _scan_log_tick(name, arguments or {}, [_TC(cached)])
                _out_text = cached
                return [_TC(cached)]
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
                _out_text = "".join(t.text for t in result)
                return result
            _scan_log_tick(name, arguments or {}, [_TC(str(result))])
            _out_text = str(result)
            return [_TC(str(result))]
        if name in ("stats_summary", "stats_status"):
            _stats_flush()  # 汇总/状态前落盘缓冲打点（协作：summary 能看到自动打点）
        if name.startswith(("pr_oracle_", "tautest_", "cae_", "stats_")):
            _ext_res = _call_ext(name, arguments or {})
            _out_text = "".join(t.text for t in _ext_res)
            return _ext_res
        raise ValueError(f"unknown tool: {name}")
    except Exception as exc:
        tel_ok = False
        tel_err = str(exc)[:200]
        return [_TC(f"Error: {exc}")]
    finally:
        # 自动打点（工具协作：每个工具调用自动记录到 stats，stats_* 自身除外）
        if not name.startswith("stats_"):
            _stats_tick(name, (time.perf_counter() - t0) * 1000,
                        in_text=json.dumps(arguments or {}, ensure_ascii=False)[:2000],
                        out_text=_out_text)
        # 遥测（阶段1）：工具调用耗时/状态/错误 → rx-telemetry（telemetry_* 自身除外防递归）
        if not name.startswith(("stats_", "telemetry_", "telemetry")):
            try:
                from telemetry_core import tick_tool
                tick_tool(name, arguments, (time.perf_counter() - t0) * 1000,
                          tel_ok, tel_err)
            except Exception:  # 监控失败静默（不拖垮工具调用）
                pass


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
            # IDE 增强 291：known_issues 语言分布（该 root 最近扫描涉及的
            # 文件语言——历史问题集中在哪些语言一眼可见）
            _ki_langs: dict[str, int] = {}
            for _l in known:
                _rt = str(_l.get("root", ""))
                _sfx = os.path.splitext(_rt)[1].lower().lstrip(".")
                if _sfx:
                    _ki_langs[_sfx] = _ki_langs.get(_sfx, 0) + 1
            if _ki_langs:
                data["known_issues_languages"] = dict(
                    sorted(_ki_langs.items(), key=lambda kv: -kv[1]))
            data["known_issues_note"] = "来自 scan-log（日志闯进调用）：该路径最近的已知问题，修复进展可查 scan_log"
            result[0] = _TC(json.dumps(data, ensure_ascii=False))
    except (json.JSONDecodeError, TypeError, AttributeError):  # 尽力而为
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
# 写采样表（2026-08-14 高压优化）：同工具上次落盘时间
_TICK_LAST: dict[str, float] = {}


def _scan_log_tick(name: str, args: dict, result: "list[types.TextContent]") -> None:
    """扫描工具结果落盘：抽取 summary + root，追加到 scan-log.jsonl（失败静默）。

    与 stats 打点互补：stats 记调用统计（ts/tool/时长），scan-log 记扫描结果
    （root/ok/summary）——"扫完的都放到日志里面"，专项目对话按 root 过滤查询。

    性能（2026-08-14 高压优化）：每工具 5 秒最多落盘 1 条——cProfile 热点
    （200 次调用 3.14s 中 _scan_log_tick 占 0.996s=32%——每次 append 都触发
    _truncate 的 stat）——降频后 known_issues 数据仍保真（5s TTL 缓存同频）。
    """
    if name not in _SCAN_LOG_TOOLS:
        return
    # 写采样：同 root 同工具 5s 内只写一条（不同 root 各自记录——防跨项目
    # 吞记录；高频调用不再放大日志 IO）
    _now = time.time()
    _key = f"{name}|{str(args.get('path', '') or args.get('root', ''))}"
    if _now - _TICK_LAST.get(_key, 0.0) < 5.0:
        return
    _TICK_LAST[_key] = _now
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
    except (json.JSONDecodeError, TypeError, AttributeError):  # 尽力而为
        pass
    root = str(args.get("path", "") or args.get("root", ""))
    scan_log_core.append_scan({"tool": name, "root": root, "ok": True, "summary": summary})


def _stats_tick(tool: str, duration_ms: float,
                in_text: str = "", out_text: str = "") -> None:
    """工具调用自动打点：内存缓冲，满 100 条或退出时批量落盘（失败静默）。

    性能约束：打点路径必须 O(1)——纯函数调用（math_ops 等）1000 次 <50ms。
    - 锁内只做 append（微秒级），绝不做文件 IO
    - flush 用快照交换 + 后台 daemon 线程异步落盘——_call 路径零阻塞
    - stats_summary/stats_status 调用前仍同步 _stats_flush() 取最新数据
    - 成本核算（2026-08-17）：tokens_in/tokens_out 由 in_text/out_text 估算
      （cost_core.estimate_tokens 延迟 import——纯函数路径零开销）
    """
    global _STATS_BUF
    try:
        if "stats_record" not in _EXT_DEFS:
            return
        record = {
            "ts": time.time(),
            "task": "unified-rx",
            "tool": tool,
            "action": tool,
            "duration_ms": duration_ms,
        }
        if in_text or out_text:
            try:
                from cost_core import estimate_tokens
                if in_text:
                    record["tokens_in"] = estimate_tokens(in_text)
                if out_text:
                    record["tokens_out"] = estimate_tokens(out_text)
            except Exception:
                pass  # 成本估算失败不影响打点
        with _STATS_LOCK:
            _STATS_BUF.append(record)
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
    except Exception:  # 尽力而为
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
    except Exception:  # 尽力而为
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
    # 阶段1（2026-08-15）：实时触发——文件改动监听（2s 指纹轮询 → 即时增量扫描）
    try:
        from realtime_watch import start_watcher
        start_watcher()
    except Exception:  # 尽力而为（监听失败不影响主服务）
        pass

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
_SCAN_LOOPS_STOP = False  # 2026-08-15：停止标志（测试隔离——置位后循环退出）


def _stop_scan_loops() -> None:
    """停止后台扫描循环（测试隔离——防线程跨测试污染 env/日志）。"""
    global _SCAN_LOOPS_STOP
    _SCAN_LOOPS_STOP = True


_spawn_self_scan_once = None  # 模块级单轮自扫入口（daemon.py 引用）


def _self_scan_once() -> None:
    """模式⑤自扫一轮：全家文件级并发 + 扩展目录并发。

    增量（用户理念：『有变动才重新扫描，无变动不重扫』）——
    `~/.unified-rx/self_scan_state.json` 记录每个文件的 (mtime_ns, size)，
    未变动的文件跳过 bug_scan，只扫变动的（省 token + 只增加动态）。
    """
    # 暴露为模块级供独立守护（daemon.py）单轮调用
    global _spawn_self_scan_once
    _spawn_self_scan_once = _self_scan_once
    try:
        import scan_log_core  # noqa: F401
    except ImportError:
        import sys as _sys
        _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import scan_log_core  # noqa: F401
    from concurrent.futures import ThreadPoolExecutor, as_completed
    files = scan_log_core.self_scan_files()

    # 增量状态：无变动不重扫（env 可覆盖路径——测试隔离/自定义）
    state_path = Path(os.environ.get(
        "UNIFIED_RX_SELF_SCAN_STATE",
        str(Path.home() / ".unified-rx" / "self_scan_state.json")))
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) \
            if state_path.exists() else {}
    except Exception:
        state = {}
    changed: list[str] = []
    for f in files:
        try:
            st = os.stat(f)
        except OSError:
            continue
        sig = [st.st_mtime_ns, st.st_size]
        if state.get(f) != sig:
            changed.append(f)
    if not changed:
        # 无变动——不重扫（理念：有变动才扫）；但落一条状态记录
        # （知识库可见心跳：确认自扫链存活，历史可查）
        scan_log_core.append_scan({
            "tool": "self_scan", "root": "self", "ok": True,
            "summary": f"self 无变动（{len(files)} 文件签名未变，跳过——增量）",
        })
        return

    def scan_one(f: str) -> None:
        try:
            d = json.loads(_call("bug_scan", {"path": f})[0].text)
            n = len(d.get("issues", [])) if isinstance(d, dict) else -1
            scan_log_core.append_scan({
                "tool": "self_scan", "root": f, "ok": n == 0,
                "summary": f"self bug_scan {os.path.basename(f)}: issues={n}",
            })
            # 清单 A（2026-08-14 用户理念『注释/占位都会挖出来』）：
            # std_check 维度（占位文字/魔法数字/命名冲突/UI 硬编码）随变更文件常驻挖
            s = json.loads(_call("std_check", {"path": f})[0].text)
            sn = len(s.get("issues", [])) if isinstance(s, dict) else -1
            scan_log_core.append_scan({
                "tool": "self_std", "root": f, "ok": sn == 0,
                "summary": f"self std_check {os.path.basename(f)}: issues={sn}",
            })
        except Exception:  # 尽力而为
            pass

    with _CONCURRENCY_SEM:
        pool = _pool()
        futs = [pool.submit(scan_one, f) for f in changed]
        for fut in as_completed(futs):
            fut.result()
    # 状态落盘（只增：已扫文件签名入 state；删除的文件下次自然消失；
    # 原子写 tmp+replace——security review LOW-2：崩溃不留坏 JSON）
    try:
        for f in changed:
            st = os.stat(f)
            state[f] = [st.st_mtime_ns, st.st_size]
        state_path.parent.mkdir(parents=True, exist_ok=True)
        _tmp = state_path.with_suffix(f".json.tmp{os.getpid()}")
        _tmp.write_text(json.dumps(state), encoding="utf-8")
        _tmp.replace(state_path)
    except Exception:  # 尽力而为
        pass
    for d in scan_log_core.self_scan_dirs():
        try:
            r = _call("bug_scan", {"path": d, "max_files": 50})[0]
            dd = json.loads(r.text)
            n = len(dd.get("issues", [])) if isinstance(dd, dict) else -1
            scan_log_core.append_scan({
                "tool": "self_scan", "root": d, "ok": n == 0,
                "summary": f"self bug_scan {os.path.basename(d)}: issues={n}",
            })
        except Exception:  # 尽力而为
            pass


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
        except Exception:  # 尽力而为
            pass
        try:
            logs = scan_log_core.query_logs(limit=50)
            roots = [l.get("root", "") for l in logs if l.get("root")]
            if roots:
                return roots[0]
        except Exception:  # 尽力而为
            pass
        for cand in (r"D:\开发\VoxelForge-Nexus", r"D:\开发\reasonix-src",
                     r"D:\开发\VoxelForge"):
            if os.path.isdir(cand):
                return cand
        return None


    def _project_scan_once() -> None:
        """模式①④ 一轮：跟随话题项目（无则最活跃）并发扫。

        增量（用户理念：『有变动才重新扫描，无变动不重扫』——省 token）：
        cb_index 变更感知——无变动的项目跳过 project_scan，只落心跳记录。
        """
        proj = _active_project()
        if not proj:
            return
        # 首轮判定：未索引（indexed=False）→ 必须全量扫（changed=[] 不代表无变动）
        try:
            st = json.loads(_call("cb_status", {"path": proj})[0].text)
            indexed = bool(st.get("indexed")) if isinstance(st, dict) else False
        except Exception:
            indexed = False
        if not indexed:
            try:
                _call("project_scan", {"path": proj, "max_files": 100})
            except Exception:  # 尽力而为
                pass
            return
        # 已索引：变更感知——无变动跳过 project_scan（省 token）
        try:
            d = json.loads(_call("cb_index", {"path": proj})[0].text)
            changed = d.get("changed", []) if isinstance(d, dict) else []
        except Exception:
            changed = []
        if not changed:
            scan_log_core.append_scan({
                "tool": "project_scan", "root": proj, "ok": True,
                "summary": "project 无变动（cb 签名未变，跳过——增量）",
            })
            return
        try:
            _call("project_scan", {"path": proj, "max_files": 100})
        except Exception:  # 尽力而为
            pass

    def _full_scan_once() -> None:
        """模式② 一轮：多项目根并发全盘扫。"""
        try:
            _call("full_scan", {"max_files": 100, "ui": False})
        except Exception:  # 尽力而为
            pass

    def _loop(name: str, interval_env: str, default: float, fn) -> None:
        """持续循环线程：首轮立即跑，之后每 interval 秒跑一轮（可停——
        2026-08-15：_SCAN_LOOPS_STOP 置位后退出——测试隔离用）。"""
        interval = float(os.environ.get(interval_env, default))
        if interval < 10:
            interval = 10  # 防 DoS：间隔下限 10s

        def runner() -> None:
            while not _SCAN_LOOPS_STOP:
                try:
                    fn()
                except Exception:  # 尽力而为
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
