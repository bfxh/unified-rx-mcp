#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""unified-rx 语义回归测试（semantic regression，用户要求：每次改完就跑）。

用法：
    python scripts/semantic_regression.py            # 全跑，退出码 0=全过
    python scripts/semantic_regression.py --json     # JSON 输出（供 CI/reports）
    python scripts/semantic_regression.py --tool math_ops  # 只跑指定工具锚点

设计（为什么是"语义"回归而不是普通冒烟）：
  - 全部断言走生产路径 server._call()（含权限检查/推测执行/扩展路由/日志打点），
    不是直接调内部函数——能抓住"注册了但分发断链"这类集成层回归（bug#1：
    ciopt_ 52 工具 manifest 有、_call 却 unknown tool）。
  - 每个工具至少一个"语义锚点"：(工具, 参数, 断言类型, 期望值)。断言类型：
      eq            输出文本精确等于期望
      contains      输出包含子串
      json_field    输出为 JSON，字段 == 期望（支持 a.b.c 路径）
      json_contains 输出为 JSON，字段是列表且含期望项
      not_error     输出不以 "Error" 开头
      error         输出以 "Error" 开头（错误契约本身也是语义）
  - 工具名一致性锚点：capability_manifest 列出的每个工具必须存在于注册表
    （静态检查，零副作用）；扩展工具全量做"可路由实调"（缺参契约类，
    秒回）——防"能力清单幻觉"（bug#1 复发）。
  - 错误契约锚点：除零/未知 action/未知工具/路径越界必须报 Error（这些是
    AI 最容易被误导的语义：把 Error 当结果）。

覆盖范围（与 pytest 互补，不重复单元断言）：
  - 核心纯函数族（math/text/sort/stat/json/prime/fib——含 rx-core Rust 桥接）
  - 文件层（fs_* 往返 + 沙盒越界拒绝）
  - 扫描工具（bug_scan 五类规则命中/干净文件零命中/close 容忍；std_check
    占位符/魔法数/密钥；hallucination_guard 三态）——挖漏洞能力的回归锚点
  - 协作层（tool_card/pipeline/parallel/agent 组合工具结构契约）
  - 扩展层（cae_lsp_position_convert 纯函数、stats_status、pr_oracle/tautest
    缺参契约、ciopt_ 52 工具全量可路由 + 代表语义）
"""
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# 沙盒：测试临时目录 + 仓库根（probes/conftest 同款做法，防 fs_* 越界报错）
_TMP = tempfile.mkdtemp(prefix="rx_semreg_")
_sb = os.environ.get("UNIFIED_RX_SANDBOX", "")
if _TMP not in _sb.split(";"):
    os.environ["UNIFIED_RX_SANDBOX"] = ";".join([x for x in _sb.split(";") if x] + [_TMP])

import server  # noqa: E402

RESULTS: list[dict] = []


def _call(tool: str, args: dict) -> str:
    """生产路径调用：_call → 权限 → 扩展路由 → 日志/遥测打点。"""
    try:
        out = server._call(tool, args)
        return "".join(getattr(t, "text", "") for t in out)
    except Exception as exc:  # noqa: BLE001 —— 任何异常都算失败（生产路径不许炸）
        return f"Error: {type(exc).__name__}: {exc}"


def check(tool: str, args: dict, kind: str, expect, desc: str = "") -> None:
    text = _call(tool, args)
    ok = False
    detail = ""
    try:
        if kind == "eq":
            ok = text == expect
            detail = f"got={text!r}" if not ok else ""
        elif kind == "contains":
            ok = expect in text
            detail = f"want {expect!r} in {text[:120]!r}" if not ok else ""
        elif kind == "not_error":
            ok = not text.startswith("Error")
            detail = f"got Error: {text[:160]}" if not ok else ""
        elif kind == "error":
            ok = text.startswith("Error")
            detail = f"want Error, got {text[:160]!r}" if not ok else ""
        elif kind == "routeable":
            # 路由可达：不是 "unknown tool" 即算通（缺参/参数校验错误恰好证明
            # 已路由到实现——bug#1 的断言形态）
            ok = "unknown tool" not in text[:80]
            detail = f"路由失败: {text[:160]}" if not ok else ""
        elif kind == "json_field":
            data = json.loads(text)
            cur = data
            for part in str(expect[0]).split("."):
                cur = cur[part]
            ok = cur == expect[1]
            detail = f"field {expect[0]}={cur!r}, want {expect[1]!r}" if not ok else ""
        elif kind == "json_contains":
            data = json.loads(text)
            lst = data
            for part in str(expect[0]).split("."):
                lst = lst[part]
            ok = any(str(x.get("name", x)) == str(expect[1]) for x in lst)
            detail = f"{expect[0]} 缺 {expect[1]!r}" if not ok else ""
    except (json.JSONDecodeError, KeyError, TypeError, IndexError, ValueError) as exc:
        ok = False
        detail = f"解析/断言异常 {type(exc).__name__}: {exc}; text={text[:200]!r}"
    RESULTS.append({"tool": tool, "kind": kind, "desc": desc, "ok": ok,
                    "detail": detail, "args": args})


def main() -> int:
    # ── 1) 核心纯函数族（含 rx-core Rust 桥接路径）──
    M = [  # (tool, args, kind, expect, desc)
        ("math_ops", {"action": "add", "a": 1, "b": 2}, "eq", "3", "add 语义"),
        ("math_ops", {"action": "sub", "a": 10, "b": 4}, "eq", "6", "sub 语义"),
        ("math_ops", {"action": "mul", "a": 6, "b": 7}, "eq", "42", "mul 语义"),
        ("math_ops", {"action": "div", "a": 9, "b": 3}, "eq", "3.0", "div 语义"),
        ("math_ops", {"action": "div", "a": 7, "b": 0}, "error", None, "除零错误契约"),
        ("math_ops", {"action": "power", "base": 2, "exponent": 10}, "eq", "1024", "power 语义"),
        ("math_ops", {"action": "sqrt", "x": 81}, "eq", "9.0", "sqrt 语义"),
        ("math_ops", {"action": "abs", "x": -5}, "eq", "5", "abs 语义"),
        ("math_ops", {"action": "factorial", "n": 6}, "eq", "720", "factorial 语义"),
        ("math_ops", {"action": "c2f", "celsius": 100}, "eq", "212.0", "c2f 语义"),
        ("math_ops", {"action": "f2c", "fahrenheit": 32}, "eq", "0.0", "f2c 语义"),
        ("math_ops", {"action": "nope"}, "error", None, "未知 action 错误契约"),
        ("text_ops", {"action": "reverse", "s": "abc"}, "eq", "cba", "reverse 语义"),
        ("text_ops", {"action": "upper", "s": "abc"}, "eq", "ABC", "upper 语义"),
        ("text_ops", {"action": "lower", "s": "AbC"}, "eq", "abc", "lower 语义"),
        ("text_ops", {"action": "palindrome", "s": "abba"}, "eq", "True", "palindrome 语义"),
        ("text_ops", {"action": "palindrome", "s": "ab"}, "eq", "False", "palindrome 负例"),
        ("sort_search", {"action": "quick_sort", "arr": [3, 1, 2]}, "eq", "[1, 2, 3]", "quick_sort 语义"),
        ("sort_search", {"action": "bubble_sort", "arr": [3, 1, 2]}, "eq", "[1, 2, 3]", "bubble_sort 语义"),
        ("sort_search", {"action": "binary_search", "arr": [1, 3, 5, 7], "target": 5}, "eq", "2", "binary_search 命中"),
        ("sort_search", {"action": "binary_search", "arr": [1, 3, 5, 7], "target": 9}, "eq", "-1", "binary_search 未命中"),
        ("stat_geo", {"action": "mean", "data": [1, 2, 3, 4]}, "eq", "2.5", "mean 语义"),
        ("stat_geo", {"action": "median", "data": [3, 1, 2]}, "eq", "2", "median 语义"),
        ("stat_geo", {"action": "circle_area", "radius": 2}, "eq", "12.566370614359172", "circle_area 语义"),
        ("stat_geo", {"action": "rect_perimeter", "length": 3, "width": 4}, "eq", "14", "rect_perimeter 语义"),
        ("json_email", {"action": "parse", "json_string": '{"a": 1}'}, "eq", '{"a": 1}', "json parse 语义"),
        ("json_email", {"action": "valid", "json_string": '{"a": 1}'}, "eq", "true", "json valid 正例（rx-core 桥接小写）"),
        ("json_email", {"action": "valid", "json_string": "{bad}"}, "eq", "false", "json valid 负例"),
        ("json_email", {"action": "email", "email": "a@b.com"}, "eq", "True", "email 正例"),
        ("json_email", {"action": "email", "email": "not-an-email"}, "eq", "False", "email 负例"),
        ("prime_list", {"action": "is_prime", "n": 17}, "eq", "true", "is_prime 正例"),
        ("prime_list", {"action": "is_prime", "n": 16}, "eq", "false", "is_prime 负例"),
        ("prime_list", {"action": "generate", "limit": 10}, "contains", "[2, 3, 5, 7]", "generate 语义"),
        ("prime_list", {"action": "unique", "lst": [1, 1, 2, 3, 3]}, "eq", "[1, 2, 3]", "unique 语义"),
        ("prime_list", {"action": "flatten", "nested_list": [1, [2, [3]]]}, "eq", "[1, 2, 3]", "flatten 语义"),
        ("fib_fibonacci", {"n": 10}, "eq", "55", "fib 语义（rx-core Rust 桥接）"),
        ("fib_fibonacci", {"n": 0}, "eq", "0", "fib 边界"),
    ]
    for t, a, k, e, d in M:
        check(t, a, k, e, d)

    # ── 2) 文件层往返 + 沙盒越界 ──
    fp = os.path.join(_TMP, "a.txt")
    check("fs_write", {"path": fp, "content": "hello", "__authorized": True},
          "not_error", None, "fs_write 成功")
    check("fs_read", {"path": fp}, "eq", "hello", "fs_read 往返语义")
    check("fs_stat", {"path": fp}, "json_field", ("exists", True), "fs_stat JSON 契约（exists）")
    check("fs_list", {"path": _TMP}, "json_contains", ("entries", "a.txt"), "fs_list 列出写入文件")
    check("fs_read", {"path": "C:/Windows/win.ini"}, "error", None, "沙盒越界拒绝")

    # ── 3) 扫描工具语义（挖漏洞能力的回归锚点——用户核心痛点）──
    bad = os.path.join(_TMP, "bad.py")
    with open(bad, "w", encoding="utf-8") as f:
        f.write("def f(a):\n"
                "    x = None\n"
                "    x.name\n"
                "    r = 1 / 0\n"
                "    s = [1, 2]\n"
                "    s[5]\n"
                "    h = open('t.txt')\n"
                "    return missing_var\n")
    check("bug_scan", {"path": bad}, "json_field", ("ok", True), "bug_scan JSON 契约")
    txt = _call("bug_scan", {"path": bad})
    try:
        rules = {i["rule"] for i in json.loads(txt)["issues"]}
    except Exception as exc:  # noqa: BLE001
        rules = {"PARSE_ERR"}
    WANT = {"divide_by_zero", "index_out_of_range", "resource_leak",
            "undefined_name", "none_deref"}
    RESULTS.append({"tool": "bug_scan", "kind": "json_field",
                    "desc": "bug_scan 五类规则全命中（挖漏洞能力锚点）",
                    "ok": rules >= WANT,
                    "detail": "" if rules >= WANT else f"缺: {sorted(WANT - rules)}",
                    "args": {"path": bad}})
    clean = os.path.join(_TMP, "clean.py")
    with open(clean, "w", encoding="utf-8") as f:
        f.write("def f(a, b):\n    return a + b\n")
    check("bug_scan", {"path": clean}, "json_field", ("issue_count", 0), "干净文件零命中")
    # 2026-08-19 智商降低排查：x[len(x)] 确定性越界必须报 error
    lenidx = os.path.join(_TMP, "lenidx.py")
    with open(lenidx, "w", encoding="utf-8") as f:
        f.write("def pick(items):\n    return items[len(items)]\n")
    check("bug_scan", {"path": lenidx}, "json_field", ("issue_count", 1),
          "x[len(x)] 确定性越界检出（error）")
    # 2026-08-19 算法演进：负索引字面量越界（AST UnaryOp）检出
    negidx = os.path.join(_TMP, "negidx.py")
    with open(negidx, "w", encoding="utf-8") as f:
        f.write("def neg():\n    s = [1, 2]\n    return s[-3]\n")
    check("bug_scan", {"path": negidx}, "json_field", ("issue_count", 1),
          "负索引字面量越界检出（error）")
    # 2026-08-19 算法演进：变量零分母（z=0 后 / z）确定性除零检出
    zerovar = os.path.join(_TMP, "zerovar.py")
    with open(zerovar, "w", encoding="utf-8") as f:
        f.write("def d():\n    z = 0\n    return 10 / z\n")
    check("bug_scan", {"path": zerovar}, "json_field", ("issue_count", 1),
          "变量零分母确定性除零检出（error）")
    close_ok = os.path.join(_TMP, "close_ok.py")
    with open(close_ok, "w", encoding="utf-8") as f:
        f.write("h = open('a')\nh.close()\nwith open('b') as g:\n    pass\n")
    txt = _call("bug_scan", {"path": close_ok})
    try:
        leak = [i for i in json.loads(txt)["issues"] if i["rule"] == "resource_leak"]
    except Exception:  # noqa: BLE001
        leak = ["PARSE_ERR"]
    RESULTS.append({"tool": "bug_scan", "kind": "json_field",
                    "desc": "显式 close/with 不误报 resource_leak",
                    "ok": not leak, "detail": f"leak={leak}" if leak else "",
                    "args": {"path": close_ok}})
    std_bad = os.path.join(_TMP, "std_bad.py")
    with open(std_bad, "w", encoding="utf-8") as f:
        f.write("TODO: 待实现\nSECRET = 'sk-abc123'\nx = 42  # magic\n")
    check("std_check", {"path": std_bad}, "json_field", ("summary.total", 1),
          "std_check 检出占位符（total≥1）")
    # 2026-08-19 智商降低排查：命名常量不误报、裸魔法数字仍报
    std_const = os.path.join(_TMP, "std_const.py")
    with open(std_const, "w", encoding="utf-8") as f:
        f.write("WINDOW_W = 1280\nTIMEOUT_SEC = 300\ndef g():\n    return [[0] * 1024]\n")
    check("std_check", {"path": std_const}, "json_field", ("summary.total", 1),
          "std_check 命名常量不误报 + 裸数字仍报（去重）")
    check("hallucination_guard", {"text": "def foo() 在 server.py:99999"},
          "contains", "refuted", "幻觉守卫 refuted 语义")
    check("hallucination_guard", {"text": "这里没有可验证声明"},
          "contains", "unverifiable", "幻觉守卫 unverifiable 语义")

    # ── 4) 协作层结构契约（组合工具 agent，旧 agent_orchestrate 已合并）──
    check("tool_card", {"name": "math_ops", "arguments": {"action": "add", "a": 1, "b": 2}},
          "json_field", ("role", "tool"), "tool_card 角色回喂契约")
    check("tool_card", {"name": "math_ops", "arguments": {"action": "add", "a": 1, "b": 2}},
          "json_field", ("ok", True), "tool_card ok 语义")
    check("agent", {"action": "roles"}, "json_field", ("ok", True), "agent 组合工具 roles 契约")
    check("parallel", {"tasks": [{"tool": "math_ops", "args": {"action": "add", "a": 1, "b": 2}},
                                 {"tool": "math_ops", "args": {"action": "mul", "a": 6, "b": 7}}]},
          "contains", "42", "parallel 并发结果汇总语义")

    # ── 5) 扩展层语义 ──
    # 几何结果缓存（7 维缓存方案维度 4 安全落地）：同文件重复 load 命中
    try:
        import geometry_tools as _gt
        _gt._MESH_CACHE.clear()
        _mesh_p = os.path.join(_TMP, "cube.obj")
        with open(_mesh_p, "w", encoding="utf-8") as _f:
            _f.write("v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nf 1 2 3 4\n")
        _m1 = _gt.load_mesh(_mesh_p)
        _m2 = _gt.load_mesh(_mesh_p)
        RESULTS.append({"tool": "load_mesh", "kind": "json_field",
                        "desc": "几何解析缓存命中（维度4 落地）",
                        "ok": bool(_m1.get("ok")) and _mesh_p in _gt._MESH_CACHE
                        and _m2.get("vertices") == _m1.get("vertices"),
                        "detail": "" if _m1.get("ok") else str(_m1),
                        "args": {"path": _mesh_p}})
    except Exception as exc:  # noqa: BLE001
        RESULTS.append({"tool": "load_mesh", "kind": "json_field",
                        "desc": "几何解析缓存", "ok": False,
                        "detail": f"{type(exc).__name__}: {exc}", "args": {}})
    check("cae_lsp_position_convert",
          {"text": "abc\ndef", "direction": "byte_to_position", "byte_offset": 4},
          "json_field", ("position.line", 1), "cae lsp byte→position 语义")
    check("cae_lsp_position_convert",
          {"text": "abc\ndef", "direction": "position_to_byte", "line": 1, "character": 0},
          "json_field", ("byte_offset", 4), "cae lsp position→byte 语义")
    check("stats_status", {}, "json_field", ("ok", True), "stats_status JSON 契约")
    check("pr_oracle_map_local", {}, "error", None, "pr_oracle 缺参错误契约")
    check("tautest_doctor", {}, "error", None, "tautest 缺参错误契约")

    # ciopt_ 全量可路由（bug#1 回归锚点：52 个工具必须能通过 _call 路由）
    try:
        import asyncio
        asyncio.run(server._build_ext_defs())
        ext = dict(server._EXT_DEFS)
    except Exception:  # noqa: BLE001
        ext = {}
    ciopt_names = sorted(n for n in ext if n.startswith("ciopt_"))
    for n in ciopt_names:
        check(n, {}, "routeable", None, "ciopt_ 可路由（bug#1 回归锚点）")
    # 代表性 ciopt 语义（6 个纯函数精确断言）
    for t, a, k, e, d in [
        ("ciopt_main_add", {"a": 2, "b": 3}, "eq", "5", "ciopt add 语义"),
        ("ciopt_math_operations_power", {"base": 2, "exponent": 10}, "eq", "1024", "ciopt power 语义"),
        ("ciopt_string_case_to_uppercase", {"s": "abc"}, "eq", "ABC", "ciopt upper 语义"),
        ("ciopt_data_validation_is_email_valid", {"email": "a@b.com"}, "eq", "True", "ciopt email 语义"),
        ("ciopt_prime_utils_is_prime", {"n": 17}, "eq", "True", "ciopt is_prime 语义"),
        ("ciopt_sorting_algorithms_quick_sort", {"arr": [3, 1, 2]}, "eq", "[1, 2, 3]", "ciopt quick_sort 语义"),
    ]:
        check(t, a, k, e, d)

    # ── 6) 工具名一致性锚点（防能力清单幻觉）──
    # 6a. 静态：manifest 列出的每个工具必须存在于注册表（零副作用）
    try:
        m = json.loads(_call("capability_manifest", {}))
        core_names = [t["name"] for t in m.get("has", {}).get("core_tools", [])]
        ext_names = [t["name"] for t in m.get("has", {}).get("ext_tools", [])]
    except Exception as exc:  # noqa: BLE001
        core_names, ext_names = [], []
        RESULTS.append({"tool": "capability_manifest", "kind": "json_field",
                        "desc": "manifest 解析", "ok": False,
                        "detail": str(exc)[:200], "args": {}})
    missing_core = [n for n in core_names if n not in server._TOOLS]
    missing_ext = [n for n in ext_names if n not in ext]
    RESULTS.append({"tool": "capability_manifest", "kind": "json_field",
                    "desc": f"manifest 工具名全在注册表（core {len(core_names)}/ext {len(ext_names)}）",
                    "ok": not missing_core and not missing_ext,
                    "detail": f"core 缺 {missing_core} / ext 缺 {missing_ext}" if (missing_core or missing_ext) else "",
                    "args": {}})
    # 6b. 扩展全量可路由实调（缺参契约类，秒回——抓"注册了但 _call 调不到"）
    route_bad = []
    for n in sorted(ext):
        r = _call(n, {})
        if "unknown tool" in r[:80]:
            route_bad.append(n)
    RESULTS.append({"tool": "_EXT_DEFS", "kind": "json_field",
                    "desc": f"扩展 {len(ext)} 工具全量可路由（bug#1 防线）",
                    "ok": not route_bad,
                    "detail": f"不可路由: {route_bad}" if route_bad else "",
                    "args": {}})

    # ── 汇总 ──
    failed = [r for r in RESULTS if not r["ok"]]
    passed = [r for r in RESULTS if r["ok"]]
    if "--json" in sys.argv:
        print(json.dumps({"total": len(RESULTS), "passed": len(passed),
                          "failed": len(failed),
                          "ciopt_route_checked": len(ciopt_names),
                          "results": RESULTS}, ensure_ascii=False, indent=1))
    else:
        print(f"语义回归：{len(passed)} passed / {len(failed)} failed"
              f"（共 {len(RESULTS)} 锚点，ciopt 可路由 {len(ciopt_names)}）")
        for r in failed:
            print(f"  FAIL {r['tool']} [{r['desc']}] {r['detail'][:200]}")
        if failed:
            print(f"\n[FAIL] 语义回归失败 {len(failed)} 项——先修再提交（pre-push 会拦截）")
        else:
            print("\n[OK] 语义回归全过")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"语义回归脚本自身异常: {type(exc).__name__}: {exc}")
        print(traceback.format_exc())
        sys.exit(2)
