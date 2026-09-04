# -*- coding: utf-8 -*-
"""tools/astscan.py —— S9 结构化扫描域：字符→token→表达式→文件，最小单元向上聚合。

方法论（用户规则 2026-08-27）：分析按算法、按小的来再大的来，结论趋向于最小单元。
文本正则层（bug_scan）只是线索流；本模块是结构化层：

- Python：标准库 ast 真语法树。节点级判定调用（Call），天然免疫注释/字符串干扰，
  成员调用（re.exec 类）与裸调用（eval(...)）在 AST 上是不同节点，不可能混淆。
- JS：手写小型管线（无三方依赖）：
    L1 词法掩码——// 与 /* */ 注释、'..' ".." `..`（含 ${} 嵌套深度）内容置空格但
       保持长度不变 ⇒ 行列号精确、后续匹配不会命中字符串/注释内部；
    L2 调用面提取——括号平衡找调用点，callee 链拆分；
    L3 分类——裸 eval/exec/execSync 命中，成员链 X.exec() 排除（RegExp.prototype.exec
       是正则方法不是动态执行）；new Function 显式命中。

输出全部是最小单元条目（file/line/col/callee），聚合统计只作汇总字段。
"""
import ast
import re

from registry import tool

_JS_SINKS_BARE = {"eval", "exec", "execSync"}
_PY_SINKS_NAME = {"eval", "exec", "compile"}
_PY_ATTR_SHELL = {"system", "popen", "Popen", "spawnSync"}
_SECRET_SHAPE = re.compile(r"\b(sk-[A-Za-z0-9_\-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16})\b")


# ---------------- Python：真 AST ----------------

def _scan_python_ast(src, fp):
    issues = []
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [{"file": fp, "line": e.lineno or 0, "col": 0, "rule": "syntax_error",
                 "detail": f"AST 解析失败: {e.msg}", "unit": "module"}]
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in _PY_SINKS_NAME:
                # S13 准确率分级：字面量参数=静态可判（info）；动态变量/表达式=真风险（med）
                kind = "literal" if all(isinstance(a, ast.Constant) for a in node.args[:1]) \
                    else "dynamic"
                issues.append({"file": fp, "line": node.lineno, "col": node.col_offset,
                               "rule": "py_dynamic_exec", "callee": fn.id,
                               "arg_kind": kind, "unit": "call",
                               # S77：词表统一 med（原 medium 被排序表当 info 沉底）
                               "severity": "info" if kind == "literal" else "med"})
            elif isinstance(fn, ast.Attribute) and fn.attr in _PY_ATTR_SHELL:
                issues.append({"file": fp, "line": node.lineno, "col": node.col_offset,
                               "rule": "shell_like_call",
                               "callee": ast.dump(fn)[:60], "unit": "call"})
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            m = _SECRET_SHAPE.search(node.value)
            if m:
                v = m.group(0)
                issues.append({"file": fp, "line": node.lineno, "col": node.col_offset,
                               "rule": "secret_literal",
                               "detail": v[:6] + "***len=" + str(len(v)), "unit": "const"})
    return issues


# ---------------- JS：词法掩码 + 括号平衡 ----------------

def _mask_js(src):
    """状态机掩码：注释/引号字符串置空格（长度不变）；模板字面量文本置空格，
    但 `${...}` 插值内部是【真执行的代码】必须保留——否则 eval 漏报。
    返回 (masked, stats)。"""
    out = list(src)
    n = len(src)
    i = 0
    strings = templates = comments = 0
    # 栈帧: {'kind':'tpl','brace':int,'in_code':bool}
    stack = []

    def in_code():
        return not stack  # 文件顶层

    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        top = stack[-1] if stack else None

        # ---- 模板文本区 ----
        if top is not None and not top["in_code"]:
            if c == "\\" and i + 1 < n:
                out[i] = out[i + 1] = " "
                i += 2
                continue
            if c == "$" and nxt == "{":
                top["in_code"] = True
                top["brace"] = 0
                i += 2
                continue
            if c == "`":
                out[i] = " "
                stack.pop()
                i += 1
                continue
            out[i] = " "
            i += 1
            continue

        # ---- 代码区（顶层或模板插值内）----
        if c == "/" and nxt == "/":
            j = src.find("\n", i)
            j = n if j < 0 else j
            out[i:j] = " " * (j - i)
            comments += 1
            i = j
        elif c == "/" and nxt == "*":
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out[i:j] = " " * (j - i)
            comments += 1
            i = j
        elif c in "\"'":
            q, j = c, i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == q or src[j] == "\n":
                    break
                j += 1
            j = min(j + 1, n)
            out[i:j] = " " * (j - i)
            strings += 1
            i = j
        elif c == "`":
            out[i] = " "
            stack.append({"kind": "tpl", "brace": 0, "in_code": False})
            templates += 1
            i += 1
        elif top is not None and top["in_code"]:
            if c == "{":
                top["brace"] += 1
            elif c == "}":
                if top["brace"] == 0:
                    top["in_code"] = False   # 保留 '}' 本身：插值边界结构，调用检测不受影响
                else:
                    top["brace"] -= 1
            i += 1
        else:
            i += 1
    return "".join(out), {"strings_masked": strings,
                          "templates_masked": templates, "comments_masked": comments}


_CALL_RE = re.compile(r"(?<![.\w$])(new\s+)?([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*\(")


def _scan_js_calls(masked, fp):
    """对掩码后文本做括号平衡的调用提取与分类。"""
    issues = []
    line_starts = [0]
    for k, ch in enumerate(masked):
        if ch == "\n":
            line_starts.append(k + 1)

    def pos_of(off):
        import bisect
        li = bisect.bisect_right(line_starts, off) - 1
        return li + 1, off - line_starts[li]

    match_iter = list(_CALL_RE.finditer(masked))
    for idx, m in enumerate(match_iter):
        is_new = bool(m.group(1))
        chain = m.group(2)
        open_off = m.end() - 1
        depth = 0
        j = open_off
        while j < len(masked):
            cj = masked[j]
            if cj in "\"'`":
                # 调用参数内字符串里还可能藏着调用——跳过整段字符串再继续数括号
                q = cj
                j += 1
                while j < len(masked) and (masked[j] != q or (j > 0 and masked[j - 1] == "\\")):
                    j += 1
            elif cj == "(":
                depth += 1
            elif cj == ")":
                depth -= 1
                if depth == 0:
                    break
            elif cj == ";" and depth <= 0:
                break
            j += 1
        if is_new and chain == "Function":
            ln, co = pos_of(m.start())
            issues.append({"file": fp, "line": ln, "col": co, "rule": "js_new_function",
                           "callee": "new Function(...)", "span_args": None, "unit": "call"})
        elif "." not in chain and chain in _JS_SINKS_BARE:
            ln, co = pos_of(m.start())
            issues.append({"file": fp, "line": ln, "col": co, "rule": "js_dynamic_exec",
                           "callee": chain, "unit": "call"})
    return issues, {"calls_total": len(match_iter)}


# ---------------- Rust：词法掩码 + 结构化信号（VoxelForge 主语言）----------------

_PANIC_CALL_RE = re.compile(r"\b(?:\.\s*)?(unwrap|expect|panic!|unreachable!|todo!|unimplemented!)\s*[(!]", )
_SAFE_IDENT = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
_MOD_TEST_ATTR = re.compile(r"#\[cfg\s*\(\s*test\s*\)\s*\]")


def _mask_rust(src):
    """Rust 词法掩码：// 与 /* */ 注释、'...' 字符串、"..." 字符串、r".." 原始字符串
    内容置空格（长度不变）。char 字面量 'a' 与生命周期 'a 靠后随字符区分：短闭合视为 char。"""
    out = list(src)
    n = len(src)
    i = 0
    strings = comments = 0
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            j = src.find("\n", i)
            j = n if j < 0 else j
            out[i:j] = " " * (j - i)
            comments += 1
            i = j
        elif c == "/" and nxt == "*":
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out[i:j] = " " * (j - i)
            comments += 1
            i = j
        elif c == "r" and nxt in ('"', "#"):
            # 原始字符串 r"..." / r#"..."#：跳过引号计数（简易，含 # 前缀）
            j = i + 1
            hashes = 0
            while j < n and src[j] == "#":
                hashes += 1
                j += 1
            if j < n and src[j] == '"':
                j += 1
                while j < n:
                    if src[j] == '"':
                        k = j + 1
                        if all(k + h < n and src[k + h] == "#" for h in range(hashes)):
                            break
                    j += 1
                j = min(j + 1, n)
                out[i:j] = " " * (j - i)
                strings += 1
                i = j
                continue
        elif c == '"':
            q, j = c, i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == q or src[j] == "\n":
                    break
                j += 1
            j = min(j + 1, n)
            out[i:j] = " " * (j - i)
            strings += 1
            i = j
        elif c == "'":
            # char 字面量 'x'（短闭合）——生命周期 'a 后无紧跟闭合引号则跳过
            j = i + 1
            closed = False
            while j < n and j < i + 5:
                if src[j] == "'":
                    closed = True
                    break
                if src[j] == "\\":
                    j += 2
                    continue
                j += 1
            if closed:
                out[i:j + 1] = " " * (j + 1 - i)
                strings += 1
                i = j + 1
            else:
                i += 1
        else:
            i += 1
    return "".join(out), {"strings_masked": strings, "comments_masked": comments}


def _scan_rust_struct(masked, src, fp):
    """结构化信号 + S12 函数级切片归属：
    - 每条 issue 归属到所在 fn（花括号深度追踪）
    - risky_fns 聚合（unwrap/unsafe 计数排序）——定位从"158 行平铺"变成"top 风险函数"
    """
    lines = masked.split("\n")
    issues = []
    fn_names = []
    unsafe_blocks = []
    in_test_mod = False
    test_mod_depth = -1
    brace_depth = 0
    fn_stack = []          # (depth_at_open, name)
    fn_risk = {}           # name -> {"unwrap": n, "unsafe": n}
    _FN_RE = re.compile(r"\bfn\s+([A-Za-z_][A-Za-z0-9_]*)")

    def emit(ln, rule, detail):
        owner = fn_stack[-1][1] if fn_stack else "<toplevel>"
        entry = fn_risk.setdefault(owner, {"unwrap": 0, "unsafe": 0})
        if rule == "rust_unsafe":
            entry["unsafe"] += 1
        elif rule == "rust_unwrap_expect":
            entry["unwrap"] += 1
        issues.append({"file": fp, "line": ln, "col": 0, "rule": rule,
                       "detail": detail, "unit": "call", "fn": owner})

    for idx, ln in enumerate(lines, 1):
        s = ln.strip()
        if not in_test_mod and _MOD_TEST_ATTR.search(s):
            in_test_mod = True
            test_mod_depth = brace_depth
        elif in_test_mod and s.startswith("}") and brace_depth <= test_mod_depth:
            in_test_mod = False
        # 先压栈：本行命中才能归属到本 fn
        mfn = _FN_RE.search(ln)
        if mfn and "{" in ln:
            fn_stack.append((brace_depth, mfn.group(1)))
            fn_names.append(mfn.group(1))
        if not in_test_mod:
            for _ in re.finditer(r"\bunsafe\b", ln):
                unsafe_blocks.append(idx)
                emit(idx, "rust_unsafe", "unsafe 块（设计信号，需人工评估不变量）")
            for m in _PANIC_CALL_RE.finditer(ln):
                name = m.group(1)
                rule = ("rust_panic_macro" if name in ("panic!", "unreachable!", "todo!", "unimplemented!")
                        else "rust_unwrap_expect")
                emit(idx, rule, m.group(0)[:40])
        new_depth = brace_depth + ln.count("{") - ln.count("}")
        # fn 体花括号平衡后深度回落到压栈值 → 出栈
        while fn_stack and fn_stack[-1][0] >= new_depth:
            fn_stack.pop()
        brace_depth = new_depth

    risky = sorted(
        ({"fn": k, **v} for k, v in fn_risk.items() if v["unwrap"] or v["unsafe"]),
        key=lambda d: -(d["unwrap"] * 2 + d["unsafe"] * 8))[:12]
    return issues, {"fn_count": len(fn_names), "unsafe_count": len(unsafe_blocks),
                    "risky_fns": risky, "fns": fn_names[:40]}


# ---------------- Rust 跨文件引用可达性（S16）----------------

_RUST_IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\b")
_RUST_KEYWORDS = {
    "fn", "let", "if", "else", "match", "return", "mod", "pub", "use", "impl",
    "self", "Self", "struct", "enum", "trait", "for", "in", "while", "loop",
    "const", "static", "type", "where", "as", "mut", "ref", "move", "dyn",
    "unsafe", "crate", "super", "true", "false", "assert", "assert_eq",
    "unsafe_fn", "async", "await", "box", "extern", "macro_rules",
}


def _rust_defs_and_refs(masked, fp, is_test_file):
    """单文件：fn 定义清单 + 每个标识符的 prod/test 引用计数（排除定义处 'fn NAME'）。"""
    defs = []                       # [{"fn","file","line","test"}]
    refs = {}                       # name -> {"prod": n, "test": n}
    lines = masked.split("\n")
    in_test_mod = False
    test_mod_depth = -1
    brace_depth = 0
    for idx, ln in enumerate(lines, 1):
        s = ln.strip()
        if not in_test_mod and _MOD_TEST_ATTR.search(s):
            in_test_mod = True
            test_mod_depth = brace_depth
        elif in_test_mod and s.startswith("}") and brace_depth <= test_mod_depth:
            in_test_mod = False
        ctx = "test" if (is_test_file or in_test_mod) else "prod"
        # 定义
        for m in re.finditer(r"\bfn\s+([A-Za-z_][A-Za-z0-9_]*)", ln):
            defs.append({"fn": m.group(1), "file": fp, "line": idx, "test": ctx == "test"})
        # 引用（跳过定义名本身；简单法：先记 fn 名占位再扣除定义命中）
        for m in _RUST_IDENT_RE.finditer(ln):
            name = m.group(1)
            if name in _RUST_KEYWORDS:
                continue
            is_def_here = re.match(r".*\bfn\s+" + re.escape(name) + r"\b", ln) and \
                m.start() > ln.rfind("fn ", 0, m.start())
            if is_def_here:
                continue
            slot = refs.setdefault(name, {"prod": 0, "test": 0})
            slot[ctx] += 1
        brace_depth += ln.count("{") - ln.count("}")
    return defs, refs


def rust_reach(rs_sources):
    """跨文件 Rust 可达性归档。

    输入 rs_sources: [(rel_fp, src, is_test_dir)]。
    策略（保守，宁可多报不可漏报）：
      prod          —— 存在 ≥1 处生产上下文引用（含 bevy add_systems 的裸标识符注册）
      test_only     —— 0 生产引用 且 ≥1 测试引用：src 里被测试专属使用的辅助函数
      unreferenced  —— 全仓零文本引用：死代码候选信号，只标不降
    已在 tests/ 目录或 cfg(test) 内的定义不参与 helper 归类（它们本来就是测试体）。
    局限如实声明：同名歧义跨 crate 不解析；宏生成调用可能漏计 → 归 unreferenced/
    test_only 前 ≥1 测试引用的要求把误降风险压到最低。
    """
    all_defs = []
    merged = {}
    for fp, src, is_test_dir in rs_sources:
        masked, _ = _mask_rust(src)
        defs, refs = _rust_defs_and_refs(masked, fp, is_test_dir)
        all_defs.extend(defs)
        for name, cnt in refs.items():
            slot = merged.setdefault(name, {"prod": 0, "test": 0})
            slot["prod"] += cnt["prod"]
            slot["test"] += cnt["test"]
    reach = {}
    helpers = []
    for d in all_defs:
        if d["test"]:
            continue
        c = merged.get(d["fn"], {"prod": 0, "test": 0})
        if c["prod"] > 0:
            v = "prod"
        elif c["test"] > 0:
            v = "test_only"
            helpers.append({"fn": d["fn"], "file": d["file"], "line": d["line"]})
        else:
            v = "unreferenced"
        reach.setdefault(d["fn"], []).append({"file": d["file"], "line": d["line"],
                                              "reach": v})
    return reach, helpers


@tool("ast_scan", "结构化扫描（S9）：Python 真 AST / JS 词法掩码+括号平衡 / Rust 结构化信号；输出最小单元条目，先小后大", "scan",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "文件或目录"},
           "max_files": {"type": "integer", "description": "上限（默认 200）"},
       },
       "required": ["path"]})
def ast_scan(path, max_files=200):
    import os
    if not os.path.exists(path):
        return {"error": f"路径不存在: {path}"}
    targets = []
    if os.path.isfile(path):
        targets = [path]
    else:
        for dp, dns, fns in os.walk(path):
            if len(targets) >= max_files:
                break
            dns[:] = [d for d in dns if d not in
                      ("node_modules", ".git", "__pycache__", ".venv", "target")]
            for fn in fns:
                ext = os.path.splitext(fn)[1].lower()
                if ext in (".py", ".js", ".mjs", ".cjs", ".rs") and len(targets) < max_files:
                    targets.append(os.path.join(dp, fn))
    if not targets:
        return {"error": "无可扫目标（仅支持 .py/.js/.mjs/.cjs/.rs）"}

    all_issues = []
    per_unit = []
    rs_sources = []
    for fp in targets:
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                src = f.read()
        except OSError:
            continue
        fp_rel = os.path.relpath(fp, path if os.path.isdir(path) else os.path.dirname(path))
        if fp.endswith(".py"):
            issues = _scan_python_ast(src, fp_rel)
            meta = {"lang": "python", "lines": src.count("\n") + 1}
        elif fp.endswith(".rs"):
            masked, mstat = _mask_rust(src)
            issues, rmeta = _scan_rust_struct(masked, src, fp_rel)
            is_test_dir = any(p == "tests" or p.startswith("tests.")
                              for p in fp_rel.replace("\\", "/").split("/")[:-1]) \
                or "\\tests\\" in fp or "/tests/" in fp
            rs_sources.append((fp_rel, src, is_test_dir))
            meta = {"lang": "rust", "lines": src.count("\n") + 1, **mstat, **rmeta}
        else:
            masked, mstat = _mask_js(src)
            issues, cstat = _scan_js_calls(masked, fp_rel)
            meta = {"lang": "javascript", "lines": src.count("\n") + 1, **mstat, **cstat}
        for it in issues:
            it["file"] = fp_rel
        all_issues.extend(issues)
        per_unit.append({"file": fp_rel, **meta})

    reach_summary = None
    if rs_sources:
        lmap, helpers = rust_reach(rs_sources)
        keyed = {(v["file"], k): v["reach"] for k, lst in lmap.items() for v in lst}
        for it in all_issues:
            r = keyed.get((it["file"], it.get("fn")))
            if r and it["rule"] in ("rust_unwrap_expect", "rust_panic_macro", "rust_unsafe"):
                it["reach"] = r
        c = {"prod": 0, "test_only": len(helpers), "unreferenced": 0}
        for lst in lmap.values():
            for v in lst:
                if v["reach"] == "prod":
                    c["prod"] += 1
                elif v["reach"] == "unreferenced":
                    c["unreferenced"] += 1
        reach_summary = {"defs_evaluated": sum(len(v) for v in lmap.values()),
                         "by_reach": c,
                         "test_only_helpers": helpers[:30],
                         "entries": sorted(
                             ({"fn": k, **v} for k, lst in lmap.items() for v in lst),
                             key=lambda d: (d["reach"] != "test_only",
                                            d["reach"] != "unreferenced", d["file"]))[:60]}

    by_rule = {}
    for it in all_issues:
        by_rule[it["rule"]] = by_rule.get(it["rule"], 0) + 1
    return {
        "files": len(per_unit),
        "total": len(all_issues),
        "by_rule": by_rule,
        "issues": all_issues,          # 最小单元层（file/line/col/unit）
        "units": per_unit[:200],       # 每 token/调用层面的统计
        "layer_note": "layer=structural（token/call 级）；上层聚合请基于 issues 自行收敛",
        "rust_reach": reach_summary,   # S16：None=无 .rs 输入；否则含 test_only_helpers
    }
