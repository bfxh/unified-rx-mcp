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
                kind = "literal" if all(isinstance(a, ast.Constant) for a in node.args[:1]) \
                    else "dynamic"
                issues.append({"file": fp, "line": node.lineno, "col": node.col_offset,
                               "rule": "py_dynamic_exec", "callee": fn.id,
                               "arg_kind": kind, "unit": "call"})
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


@tool("ast_scan", "结构化扫描（S9）：Python 真 AST / JS 词法掩码+括号平衡；输出最小单元条目，先小后大", "scan",
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
                if ext in (".py", ".js", ".mjs", ".cjs") and len(targets) < max_files:
                    targets.append(os.path.join(dp, fn))
    if not targets:
        return {"error": "无可扫目标（仅支持 .py/.js/.mjs/.cjs）"}

    all_issues = []
    per_unit = []
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
        else:
            masked, mstat = _mask_js(src)
            issues, cstat = _scan_js_calls(masked, fp_rel)
            meta = {"lang": "javascript", "lines": src.count("\n") + 1, **mstat, **cstat}
        for it in issues:
            it["file"] = fp_rel
        all_issues.extend(issues)
        per_unit.append({"file": fp_rel, **meta})

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
    }
