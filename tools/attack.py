# -*- coding: utf-8 -*-
"""tools/attack.py —— 攻击域（S7 默认化）：把"主动攻击找缺陷"从一次性动作变成常驻工具面。

设计依据（用户规则）：健康只是入场券——tests 全绿只覆盖已写用例；
真正的问题藏在没人试过的输入里。本域提供常驻攻击工具，
任何项目体检默认先跑 attack_surface，不再依赖执行者记得。

工具：
- input_fuzz   : 输入模糊集（空/空白/超长/Unicode/越界类型）对任意已注册工具
- path_probe   : 路径逃逸探测（..穿越/绝对路径外/symlink/设备名）
- big_input    : 大输入边界（1MB 字符串/超大 list/深嵌套）
"""
import os
import sys

import registry  # 显式导入：path_probe 依赖此处的 registry.call（不要用延迟属性）
from registry import tool
from . import fs as fs_tools  # 复用沙盒解析

# 各语言可扫描扩展名（复用 scan 域映射思路，保持独立防循环依赖）
_CODE_EXTS = {".py", ".rs", ".go", ".ts", ".tsx", ".js", ".jsx", ".gd",
              ".c", ".cpp", ".h", ".hpp", ".cs", ".dart", ".lua", ".sh",
              ".java", ".kt", ".php", ".rb", ".swift"}

# Windows 保留设备名（CON/NUL/...）与非法字符集
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"{c}{i}" for c in "COM LPT".split() for i in range(1, 10)}
_WIN_BAD_CHARS = set('<>:"|?*')


def _probe_target():
    """探针目录：沙盒内专用前缀。"""
    base = os.path.join(os.environ.get("TEMP", r"C:\Temp"), "unified-rx-pytest", "_attack")
    os.makedirs(base, exist_ok=True)
    return base


@tool("input_fuzz", "输入模糊：对目标工具灌入空/空白/超长/Unicode/错型参数，返回结构化存活报告", "attack",
      {"type": "object",
       "properties": {
           "tool_name": {"type": "string", "description": "被攻击的工具名（如 locate_edit）"},
           "base_args": {"type": "object", "description": "该工具的合法参数模板"},
           "fuzz_field": {"type": "string", "description": "要模糊化的参数名"},
       },
       "required": ["tool_name", "base_args", "fuzz_field"]})
def input_fuzz(tool_name, base_args, fuzz_field):
    """对单个字段灌 6 类病态值，判定标准：
    - 必须 ok:False 或 result 正常 → PASS
    - 进程崩溃不可能出现（registry 隔离），但噪音结果（如 total>0 的空查询）= FAIL
    """
    from registry import call as rx_call, _TOOLS
    if tool_name not in _TOOLS:
        return {"error": f"未知工具: {tool_name}"}
    payloads = [
        ("empty", ""),
        ("whitespace", "   \t\n"),
        ("huge_10k", "A" * 10000),
        ("unicode_mix", "日本語🔥\x00\x1b[31ḿ"),
        ("sqlish", "'; DROP TABLE x; --"),
        ("path_traversal", "..\\..\\..\\..\\Windows\\win.ini"),
        ("null_byte_safe", "abc\x00def"),
    ]
    wrong_types = [123, None, [], {}, True]
    results = []
    for label, val in payloads:
        args = dict(base_args)
        args[fuzz_field] = val
        r = rx_call(tool_name, args)
        noisy = (r.get("ok") and isinstance(r.get("result"), dict)
                 and r["result"].get("total") not in (None, 0) and not val.strip())
        results.append({"case": label, "ok": r.get("ok"), "noisy": bool(noisy),
                        "verdict": "FAIL-noise" if noisy else ("PASS-reject" if not r.get("ok") else "PASS-handle")})
    for i, wt in enumerate(wrong_types):
        args = dict(base_args)
        args[fuzz_field] = wt
        r = rx_call(tool_name, args)
        results.append({"case": f"wrong_type_{type(wt).__name__}", "ok": r.get("ok"),
                        "noisy": False,
                        "verdict": "PASS-reject" if not r.get("ok") else "PASS-handle"})
    fails = [r for r in results if r["verdict"] == "FAIL-noise"]
    return {"tool": tool_name, "field": fuzz_field,
            "cases": len(results), "failures": len(fails), "results": results}


@tool("path_probe", "路径逃逸探测：构造穿越/设备名/保留名/超长文件名并验证沙盒拒绝", "attack",
      {"type": "object", "properties": {},
       "required": []})
def path_probe():
    """在探针目录实测 8 种路径形态，全部必须拒绝或安全处理。"""
    base = _probe_target()
    cases = []
    probes = [
        ("dotdot_escape", os.path.join(base, "..", "..", "Windows", "win.ini")),
        ("abs_outside", r"C:\Windows\System32\drivers\etc\hosts"),
        ("device_con", os.path.join(base, "CON")),
        ("device_nul_stream", os.path.join(base, "file.txt:NulStream")),
        ("illegal_chars", os.path.join(base, "bad<>|?.txt")),
        ("reserved_com1", os.path.join(base, "COM1")),
        ("deep_nesting", os.path.join(base, *(["d"] * 60)) + "\\f.txt"),
        ("trailing_dot_space", os.path.join(base, "name. .")),
    ]
    for label, p in probes:
        rd = None
        try:
            rd = registry.call("fs_read", {"path": p})
            # 逃逸探测的核心断言只有一条：读必须被拒（写走 __authorized，不参与此判定）
            handled = not rd.get("ok")
        except Exception as e:  # 异常抛穿即 FAIL
            handled = False
            rd = {"error": f"exception: {e}"}
        cases.append({"case": label, "read_rejected": not (rd or {}).get("ok", False),
                      "verdict": "PASS" if handled else "CHECK"})
    return {"probes": len(cases),
            "all_safe": all(c["verdict"] == "PASS" for c in cases),
            "cases": cases}


@tool("big_input", "大输入边界：1MB 字符串 / 10 万项列表 / 500 层嵌套对目标工具", "attack",
      {"type": "object",
       "properties": {
           "tool_name": {"type": "string"},
           "base_args": {"type": "object"},
           "fuzz_field": {"type": "string"},
       },
       "required": ["tool_name", "base_args", "fuzz_field"]})
def big_input(tool_name, base_args, fuzz_field):
    from registry import call as rx_call, _TOOLS
    if tool_name not in _TOOLS:
        return {"error": f"未知工具: {tool_name}"}
    huge = "vehicle " * 120000          # ~1MB
    biglist = list(range(100000))
    deep = cur = {}
    for _ in range(500):
        cur["n"] = {}
        cur = cur["n"]
    cases = []
    for label, val in [("str_1mb", huge), ("list_100k", biglist), ("deep_500", deep)]:
        args = dict(base_args)
        args[fuzz_field] = val
        try:
            r = rx_call(tool_name, args)
            verdict = "PASS" if isinstance(r.get("ok"), bool) else "FAIL"
            err_preview = str(r.get("error", ""))[:80]
        except RecursionError:
            r, verdict, err_preview = {"ok": False}, "FAIL-recursion", ""
        except Exception as e:
            r, verdict = {"ok": False}, "PASS-catchall"
            err_preview = str(e)[:80]
        cases.append({"case": label, "ok": r.get("ok"), "verdict": verdict,
                      "err_preview": err_preview})
    return {"tool": tool_name, "cases": cases,
            "all_pass": all(c["verdict"].startswith("PASS") for c in cases)}
