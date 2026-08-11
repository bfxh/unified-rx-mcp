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
_active_proj = os.environ.get("UNIFIED_RX_PROJECT", "").strip()
if _active_proj and _active_proj not in _sandbox_roots:
    _sandbox_roots.append(_active_proj)
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


# ── 组合工具（2026-08-11 去重重构：29 单工具 → 6 组合，action 分发）──
# 背景：原 29 个纯函数单工具与外部 ci-optimization MCP 功能重复（AI 报告 45 冲突）。
# 方案：保留全部逻辑为 _m_* 内部函数，对外仅暴露 6 个组合工具 + fib_fibonacci，
# 工具数 69 → 47，能力零丢失（旧名不再暴露，如需兼容可在 _ALIASES 加映射）。

_MATH_ACTIONS = {
    "add": lambda a: str(a["a"] + a["b"]),
    "sub": lambda a: str(a["a"] - a["b"]),
    "mul": lambda a: str(a["a"] * a["b"]),
    "div": _m_math_div,
    "power": _m_math_power,
    "sqrt": _m_math_sqrt,
    "abs": _m_math_abs,
    "factorial": _m_math_factorial,
    "c2f": _m_conv_c2f,
    "f2c": _m_conv_f2c,
}


def _tool_math_ops(args: dict):
    """数学运算组合：add/sub/mul/div/power/sqrt/abs/factorial + 温度换算 c2f/f2c。"""
    action = args.get("action")
    fn = _MATH_ACTIONS.get(action)
    if fn is None:
        raise ValueError("未知 action: %s（可选 %s）" % (action, sorted(_MATH_ACTIONS)))
    return fn(args)


_TEXT_ACTIONS = {
    "reverse": _m_str_reverse,
    "upper": _m_str_upper,
    "lower": _m_str_lower,
    "palindrome": _m_str_palindrome,
}


def _tool_text_ops(args: dict):
    """文本运算组合：reverse/upper/lower/palindrome。"""
    action = args.get("action")
    fn = _TEXT_ACTIONS.get(action)
    if fn is None:
        raise ValueError("未知 action: %s（可选 %s）" % (action, sorted(_TEXT_ACTIONS)))
    return fn(args)


_SORT_SEARCH_ACTIONS = {
    "quick_sort": _m_sort_quick,
    "bubble_sort": _m_sort_bubble,
    "binary_search": _m_search_binary,
}


def _tool_sort_search(args: dict):
    """排序与查找组合：quick_sort/bubble_sort/binary_search。"""
    action = args.get("action")
    fn = _SORT_SEARCH_ACTIONS.get(action)
    if fn is None:
        raise ValueError("未知 action: %s（可选 %s）" % (action, sorted(_SORT_SEARCH_ACTIONS)))
    return fn(args)


_STAT_GEO_ACTIONS = {
    "mean": _m_stat_mean,
    "median": _m_stat_median,
    "circle_area": _m_geo_circle,
    "rect_perimeter": _m_geo_rect,
}


def _tool_stat_geo(args: dict):
    """统计与几何组合：mean/median/circle_area/rect_perimeter。"""
    action = args.get("action")
    fn = _STAT_GEO_ACTIONS.get(action)
    if fn is None:
        raise ValueError("未知 action: %s（可选 %s）" % (action, sorted(_STAT_GEO_ACTIONS)))
    return fn(args)


_JSON_EMAIL_ACTIONS = {
    "parse": _m_json_parse,
    "valid": _m_json_valid,
    "email": _m_valid_email,
}


def _tool_json_email(args: dict):
    """JSON 与校验组合：parse/valid/email。"""
    action = args.get("action")
    fn = _JSON_EMAIL_ACTIONS.get(action)
    if fn is None:
        raise ValueError("未知 action: %s（可选 %s）" % (action, sorted(_JSON_EMAIL_ACTIONS)))
    return fn(args)


_PRIME_LIST_ACTIONS = {
    "is_prime": _m_prime_is_prime,
    "generate": _m_prime_generate,
    "unique": _m_list_unique,
    "flatten": _m_list_flatten,
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


def _tool_full_scan(args: dict) -> "list[types.TextContent]":
    """全盘扫：对多个项目根**并发**跑 project_scan（每个项目四路并行），
    全部完成汇总 + 落盘 scan-log。缺省扫常见项目根，可显式传 roots 列表。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    roots = args.get("roots") or _FULL_SCAN_DEFAULT_ROOTS
    max_files = int(args.get("max_files", 100))
    ui = bool(args.get("ui", True))
    results = {"roots": roots, "projects": [], "errors": []}

    def scan_project(root: str) -> None:
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
    "fib_fibonacci": (_m_fib_fibonacci, _schema({"n": _S("integer", "≤20000")}, ["n"]), "斐波那契第 n 项"),
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
    "pipeline": (_tool_pipeline, _schema({
        "preset": _S("string", "预设配方：audit_repo/guard_text/learn/locate_context（一次调用跑完整流程，减少调用轮次）"),
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
        if name in _TOOLS:
            fn, _, _ = _TOOLS[name]
            result = fn(arguments or {})
            if isinstance(result, list):
                _scan_log_tick(name, arguments or {}, result)
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


# 扫描类工具：调用完成自动落盘 scan-log.jsonl（常驻自扫日志，专项目对话可查）
_SCAN_LOG_TOOLS = {"bug_scan", "std_check", "vuln_scan", "ui_check",
                   "cb_scan", "cb_index", "hallucination_guard", "locate_edit",
                   "project_scan", "full_scan"}


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

    # 打开阵地即自扫（后台线程，不阻塞启动）：工具常驻运行，扫自己一遍，
    # 结果落盘 scan-log.jsonl（"包括它自己也会扫自己，扫完的都放到日志里面"）。
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


def _spawn_self_scan() -> None:
    """后台自扫 + 活跃项目扫描：常驻启动即跑，互不打扰，结果落盘。

    五种常态化扫描模式（全部高并发）：
      ① 跟随话题项目：UNIFIED_RX_PROJECT 指定 → project_scan 并发扫
      ② 全盘扫：full_scan 工具（多项目根并发）
      ③ 被 RX 调用：_scan_log_tick 调用即记（每次工具调用自动落盘）
      ④ 最活跃就扫：stats.json 统计调用最多的项目 → 并发扫
      ⑤ 扫自己：全家自扫（core+scripts+lse-engine 文件级并发 + vendor 扩展目录并发）
    守护线程 + 失败静默——绝不影响 MCP 协议层。CI/测试跳过
    （UNIFIED_RX_SKIP_SELF_SCAN=1）。
    """
    if os.environ.get("UNIFIED_RX_SKIP_SELF_SCAN", "") == "1":
        return
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

    def _do() -> None:
        try:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            # 1) 自扫全家（模式⑤，高并发：文件级并行跑 bug_scan）
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
            # 1b) 扩展目录并发扫（vendor/extensions/*）
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
            # 2) 最活跃项目并发扫描（模式④：互不打扰，独立线程跑 project_scan）
            proj = _active_project()
            if proj:
                try:
                    _call("project_scan", {"path": proj, "max_files": 100})
                except Exception:
                    pass
        except Exception:
            pass  # 自扫失败静默

    threading.Thread(target=_do, daemon=True, name="rx-self-scan").start()


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
