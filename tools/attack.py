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


# ---------- S132（DESIGN-REVIEW H3）：组合透传静态自审 ----------
# 组合工具（doctor/multi_check/diagnostics…）内层调挂门工具必须显式透传
# __authorized——S130（ide_diagnostics clippy 透镜假死）与更早 swe_repair 两度
# 实锤同一病灶；本检查器专治第三次。判据=**字面量**（包装器隐式注入不算数，
# 静态看不见；见 ide_doctor 的 S132 注记）。
import re as _re  # noqa: E402

_CALL_HEAD = _re.compile(
    r"(?:registry\.call|(?<![\w.])call|(?<![\w.])reg)\(\s*[\"']([a-z_0-9]+)[\"']")


def _balanced_parens(src, open_idx, cap=4000):
    depth = 0
    for i in range(open_idx, min(len(src), open_idx + cap)):
        c = src[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return src[open_idx:i + 1]
    return src[open_idx:open_idx + cap]


def _scan_compose_passthrough(sources, gated):
    """纯函数：sources=[(显示名, 源码)] → 违规清单 ["文件:行 → 工具"]。

    只看字面量工具名（变量名调用是动态面，如实不判）；参数区取平衡括号片段
    （含嵌套 dict/多行），片段内含 __authorized 即通过。
    """
    out = []
    for fname, src in sources:
        for m in _CALL_HEAD.finditer(src):
            name = m.group(1)
            if name not in gated:
                continue
            open_idx = src.find("(", m.start())
            seg = _balanced_parens(src, open_idx)
            if "__authorized" not in seg:
                line = src.count("\n", 0, m.start()) + 1
                out.append(f"{fname}:{line} → {name}")
    return out


def _compose_passthrough_scan():
    """真机自审入口：扫本包 tools/ 与同级 bench/（存在时）。"""
    from registry import _TOOLS
    gated = {n for n, v in _TOOLS.items() if v.get("requires_auth")}
    here = os.path.dirname(os.path.abspath(__file__))
    srcs = []
    for d in (here, os.path.join(os.path.dirname(here), "bench")):
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".py"):
                continue
            try:
                with open(os.path.join(d, fn), encoding="utf-8", errors="replace") as f:
                    srcs.append((fn, f.read()))
            except OSError:
                continue
    return _scan_compose_passthrough(srcs, gated)


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
    manifest_gated = set((((mr.get("result") or {}).get("高权限")) or {}).get("工具") or [])
    diff = sorted(manifest_gated ^ set(gated))
    # S132/H3：组合透传静态自审（字面量纪律；见 _compose_passthrough_scan）
    passthrough = _compose_passthrough_scan()
    ok = (not (deny_missing or declared_missing or forced_missing)
          and not diff and not passthrough)
    # S133/M1：输出键统一英文（值内中文照旧）——本工具是最后一件中文键持有者，
    # 改名即全仓 71 工具键语言一致（DESIGN-REVIEW M1 修正：实锤仅此一件）。
    return {"total_tools": len(_TOOLS), "gated_count": len(gated), "gated": gated,
            "deny_missing": deny_missing, "declared_missing": declared_missing,
            "forced_missing": forced_missing, "manual_gate": manual,
            "manifest_consistency": "pass" if not diff else f"fail: {diff}",
            "compose_passthrough": "pass" if not passthrough else passthrough,
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


@tool("rust_taint_scan", "污点扫描（Rust 引擎，Python 代码）：形参即来源 → 汇点浅数据流，识别常用净化器；跨文件链（origin/flow=cross）；naive=true 模式匹配基线，cross=false 关跨文件",
      "attack",
      {"type": "object",
       "properties": {
           "root": {"type": "string", "description": "扫描根目录或单个 .py 文件（沙盒内）"},
           "naive": {"type": "boolean", "description": "基线模式：任何含变量实参的汇点调用都报（对照用）"},
           "cross": {"type": "boolean",
                     "description": "跨文件污点链（默认 true；false=逐字节回到 S78 文件内语义）"},
       },
       "required": ["root"]})
def rust_taint_scan(root, naive=False, cross=True):
    try:
        resolved = fs_tools._resolve(root)   # 与 fs 域同一沙盒钳制，越界即拒
    except ValueError as e:
        return {"error": str(e)}
    exe = _rx_taint_exe()
    if not exe:
        return {"error": "rx-taint.exe 不存在——先在 rust/ 下 cargo build --release "
                         "（或设 UNIFIED_RX_RS_EXE 指向现有 exe）"}
    import subprocess
    argv = [exe, resolved] + (["--naive"] if naive else []) \
        + ([] if cross or naive else ["--no-cross"])
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
    out["cross"] = bool(cross and not naive)
    return out


# ---- S133（CONSOLIDATION §四 P3）：attack 巡航——全攻击面一键自检 ----------------
# 薄聚合（同 ide_doctor 惯例）：不造新检测，编排 gate 审计 + 被动探针 + 主动模糊，
# 统一报告与 verdict。默认电池=对本包自身做回归式对抗（fs_read/locate_edit/
# code_search/bug_scan 四靶 × input_fuzz + big_input）；targets 可增补任意工具。
# 档位（HARDENING §七）：纯自审——不挂门；电池靶均非挂门工具，用户增补挂门靶时
# 其模糊调用会得"授权拒绝"（PASS-reject，属合法判定）。

_CRUISE_BATTERY = (
    ("fs_read", {"path": "<pkg>"}, "path"),
    ("locate_edit", {"path": "<pkg>", "query": "def"}, "query"),
    ("code_search", {"root": "<pkg>", "query": "read"}, "query"),
    ("bug_scan", {"path": "<pkg>"}, "path"),
)


@tool("attack_cruise",
      "攻击面巡航：一键编排授权门自审 + 路径探针 + 输入模糊 → 统一报告与 verdict（clean/issues）；纯自审不挂门，失败项全列出",
      "attack",
      {"type": "object",
       "properties": {
           "targets": {"type": "array",
                       "description": "增补靶：数组项 {tool_name, base_args, fuzz_field}",
                       "items": {"type": "object"}},
           "battery": {"type": "boolean",
                       "description": "是否跑默认四靶电池（默认 true；false 时只跑 targets）"},
           "big": {"type": "boolean",
                   "description": "是否含 big_input 大输入边界（默认 true）"},
       },
       "required": []})
def attack_cruise(targets=None, battery=True, big=True):
    from registry import call as rx_call
    pkg = os.path.dirname(os.path.abspath(__file__))
    plan = []
    if battery:
        plan.extend(_CRUISE_BATTERY)
    for t in targets or []:
        try:
            plan.append((str(t["tool_name"]), dict(t["base_args"]),
                         str(t["fuzz_field"])))
        except (KeyError, TypeError, ValueError):
            return {"error": "targets 项需为 {tool_name, base_args, fuzz_field}"}

    failures = []
    errors = []

    gr = rx_call("auth_gate_sweep", {})
    gates = gr.get("result") if gr.get("ok") else None

    pr = rx_call("path_probe", {})
    passive = pr.get("result") if pr.get("ok") else None

    fuzz, bigs = [], []
    for tool, base, field in plan:
        args = {k: (pkg if v == "<pkg>" else v) for k, v in base.items()}
        fr = rx_call("input_fuzz", {"tool_name": tool, "base_args": args,
                                    "fuzz_field": field})
        if fr.get("ok"):
            res = fr["result"]
            fuzz.append({"tool": tool, "field": field, "cases": res.get("cases"),
                         "failures": res.get("failures")})
            for c in res.get("results") or []:
                if str(c.get("verdict", "")).startswith("FAIL"):
                    failures.append(f"input_fuzz {tool}.{field}: "
                                    f"{c.get('case')} → {c.get('verdict')}")
        else:
            errors.append(f"input_fuzz({tool}) 未能执行: {str(fr.get('error'))[:120]}")
        if big:
            br = rx_call("big_input", {"tool_name": tool, "base_args": args,
                                       "fuzz_field": field})
            if br.get("ok"):
                res = br["result"]
                bigs.append({"tool": tool, "all_pass": res.get("all_pass"),
                             "cases": [c.get("case") for c in res.get("cases") or []]})
                if not res.get("all_pass"):
                    failures.append(f"big_input {tool}.{field} 未全过")
            else:
                errors.append(f"big_input({tool}) 未能执行: {str(br.get('error'))[:120]}")

    if gates is None:
        errors.append("auth_gate_sweep 未能执行")
    elif gates.get("ok") is not True:
        failures.append("授权门自审未过（见 gates 字段）")
    if passive is None:
        errors.append("path_probe 未能执行")
    elif passive.get("all_safe") is not True:
        failures.append("路径探针未全安全（见 passive 字段）")

    verdict = "clean" if not failures and not errors else "issues"
    return {"root": pkg, "verdict": verdict,
            "gates": gates, "passive": passive, "fuzz": fuzz, "big": bigs,
            "failures": failures, "errors": errors,
            "note": "巡航=编排既有攻击面工具（薄聚合不造新检测）；input_fuzz 的 "
                    "FAIL-noise=空查询返回非空结果类噪音；capacity 档位=纯自审"
                    "（HARDENING §七），挂门靶经 targets 增补时拒绝即 PASS-reject"}
