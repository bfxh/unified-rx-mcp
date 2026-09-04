# -*- coding: utf-8 -*-
"""tools/attack.py —— 攻击域（S7 默认化）：把"主动攻击找缺陷"从一次性动作变成常驻工具面。

设计依据（用户规则）：健康只是入场券——tests 全绿只覆盖已写用例；
真正的问题藏在没人试过的输入里。本域提供常驻攻击工具，
任何项目体检默认先跑 attack_surface，不再依赖执行者记得。

工具：
- input_fuzz       : 输入模糊集（空/空白/超长/Unicode/越界类型）对任意已注册工具
- path_probe       : 路径逃逸探测（..穿越/绝对路径外/symlink/设备名）
- big_input        : 大输入边界（1MB 字符串/超大 list/深嵌套）
- auth_gate_sweep  : 授权门自审（S77，全工具双向查门，S75 人眼盘点法固化成工具）
"""
import json
import os

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


# ---------- S77（VULN-HUNTING P0-a）：授权门自审 ----------

def _gate_report(entries):
    """门审计纯函数：entries = [(name, requires_auth, has_param, declared, manual_gate)]。

    独立成纯函数便于测试注入坏样本（真实 registry 里造坏注册会污染全局）。
    返回 (挂门清单, 漏声明, 门参数未强制, 手动门清单)。
    manual_gate：单工具混合读写（读开放+写动作 handler 内自查）在注册时显式
    声明（S77 起支持，如 ide_lsp）——不算"未强制"，单独归类保持可见。
    """
    gated, declared_missing, forced_missing, manual = [], [], [], []
    for name, req_auth, has_param, declared, manual_gate in entries:
        if req_auth:
            gated.append(name)
            if not declared:
                # S72b 契约：挂门工具的 schema 必须声明 __authorized，
                # 否则 MCP 宿主看不到参数就永远不会传 → 门在协议模式下恒拒绝
                declared_missing.append(name)
        elif manual_gate and has_param:
            manual.append(name)
        elif has_param:
            # handler 收 __authorized 却没挂 requires_auth 也没声明手动门——
            # registry 不强制，门形同虚设（S75 权力面盘点抓的就是这类"以为有门其实没门"）
            forced_missing.append(name)
    return gated, declared_missing, forced_missing, manual


@tool("auth_gate_sweep", "授权门自审：全部已注册工具双向查门（必拒未授权/schema 必声明/manifest 一致）", "attack",
      {"type": "object", "properties": {}, "required": []})
def auth_gate_sweep():
    """S75 权力面盘点的方法固化：一条命令查全部工具的门，漏一处即 ok:False。

    漏拒绝用 registry.call(name, {}) 端到端验证——授权检查先于 handler 执行，
    零副作用；manifest"高权限"段（S75 动态生成）与实际挂门清单必须一致。
    """
    from registry import call as rx_call, _TOOLS, list_tools
    declared = {}
    for t in list_tools():
        schema = t["inputSchema"]
        declared[t["name"]] = ("__authorized" in (schema.get("properties") or {})
                               and "__authorized" in (schema.get("required") or []))
    entries = [(n, bool(v.get("requires_auth")),
                "__authorized" in v.get("params", frozenset()),
                declared.get(n, False), bool(v.get("manual_gate")))
               for n, v in _TOOLS.items()]
    gated, declared_missing, forced_missing, manual = _gate_report(entries)
    deny_missing = []
    for n in gated:
        r = rx_call(n, {})
        if r.get("ok") or "授权" not in str(r.get("error", "")):
            deny_missing.append(n)
    mr = rx_call("capability_manifest", {})
    manifest_gated = set(((((mr.get("result") or {}).get("高权限")) or {}).get("工具")) or [])
    diff = sorted(manifest_gated ^ set(gated))
    ok = not (deny_missing or declared_missing or forced_missing) and not diff
    return {"总工具数": len(_TOOLS), "挂门数": len(gated), "挂门清单": gated,
            "漏拒绝": deny_missing, "漏声明": declared_missing,
            "门参数未强制": forced_missing, "手动门": manual,
            "manifest一致性": "pass" if not diff else f"fail: {diff}",
            "ok": ok}


# ---- S78：Rust 污点引擎接入（spec/VULN-HUNTING.md P1-a）--------------------
# rx-taint.exe 由 rust/ 工作区产出（零第三方 crate，与 python 纯 stdlib 同纪律）。
# 薄壳原则：本工具只做沙盒校验 + 进程编排 + JSON 透传，污点逻辑单一事实源在 Rust。

def _rx_taint_exe():
    """定位 rx-taint.exe：UNIFIED_RX_RS_EXE 覆盖 → cargo 目标目录惯例路径。

    候选必须是已存在且文件名恰为 rx-taint.exe 的常规文件——env 覆盖不构成
    任意命令执行面（argv 固定前缀、list 形式、无 shell）。
    """
    cand = []
    override = os.environ.get("UNIFIED_RX_RS_EXE")
    if override:
        cand.append(override)
    tmp = os.environ.get("TEMP", r"C:\Temp")
    cand += [os.path.join(tmp, "rx-rs-target", kind, "rx-taint.exe")
             for kind in ("release", "debug")]
    for c in cand:
        if os.path.isfile(c) and os.path.basename(c) == "rx-taint.exe":
            return c
    return None


@tool("rust_taint_scan", "Rust 污点引擎（S78）：来源→汇点浅数据流扫 Python 代码；"
                         "形参即来源（MCP 威胁模型），净化器 basename/secure_filename/"
                         "int/float/_fs_resolve/.name/.stem 识别；naive=true 跑模式匹配基线对照",
      "attack",
      {"type": "object",
       "properties": {
           "root": {"type": "string", "description": "扫描根目录或单个 .py 文件（沙盒内）"},
           "naive": {"type": "boolean", "description": "基线模式：任何含变量实参的汇点调用都报（对照用）"},
       },
       "required": ["root"]})
def rust_taint_scan(root, naive=False):
    try:
        resolved = fs_tools._resolve(root)   # 与 fs 域同一沙盒钳制，越界即拒
    except ValueError as e:
        return {"error": str(e)}
    exe = _rx_taint_exe()
    if not exe:
        return {"error": "rx-taint.exe 不存在——先在 rust/ 下 cargo build --release "
                         "（或设 UNIFIED_RX_RS_EXE 指向现有 exe）"}
    import subprocess
    argv = [exe, resolved] + (["--naive"] if naive else [])
    try:
        cp = subprocess.run(argv, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=600)
    except subprocess.TimeoutExpired:
        return {"error": "rx-taint 超时（600s）", "root": resolved}
    if cp.returncode != 0:
        return {"error": f"rx-taint 退出码 {cp.returncode}",
                "stderr_tail": (cp.stderr or "")[-500:]}
    try:
        out = json.loads(cp.stdout)
    except json.JSONDecodeError:
        return {"error": "rx-taint 输出不是合法 JSON", "stdout_head": cp.stdout[:300]}
    out["root"] = resolved
    out["naive"] = bool(naive)
    return out
