# -*- coding: utf-8 -*-
"""跨函数 taint：调用图 + 函数内 taint 合并（CPG 概念轻量版）。

同文件内：函数 A 调 B 且 A 的污点变量作为 B 的参数 → B 对应形参污染
（1 层传播——确定性调用边分析，不跨文件）。
用后即删（并入 bug_scan_core 后删除本文件）。
"""

import ast


def cross_taint_scan(tree: ast.AST, path: str, lines: list,
                     taint_results: list) -> None:
    """在函数内 taint 结果上做调用边传播。

    taint_results: py_taint_scan 已产出的 taint_flow issue 列表。
    传播规则：调用方 f 内 tainted 变量传给被调 g 的形参 i →
    g 内该形参参与 sink 调用时补报（跨函数 1 层）。
    """
    # 收集函数签名（name -> [params]）
    fns: dict[str, list[str]] = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fns[n.name] = [a.arg for a in n.args.args]
    # 每个函数：污点变量 = 形参 + 函数内 source 赋值（args.get/input）
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        tainted = {a.arg for a in fn.args.args}
        for st in ast.walk(fn):
            if isinstance(st, ast.Assign):
                _v = st.value
                if isinstance(_v, ast.Call):
                    _fnv = _v.func.attr if isinstance(_v.func, ast.Attribute) else (
                        _v.func.id if isinstance(_v.func, ast.Name) else "")
                    _objv = getattr(getattr(_v.func, "value", None), "id", "") \
                        if isinstance(_v.func, ast.Attribute) else ""
                    if _fnv in ("get", "input", "getenv") \
                            and _objv in ("", "args", "os", "sys"):
                        for t in st.targets:
                            if isinstance(t, ast.Name):
                                tainted.add(t.id)
        for st in ast.walk(fn):
            if not isinstance(st, ast.Call):
                continue
            # callee：模块级函数（Name）或对象方法（Attribute.attr）
            if isinstance(st.func, ast.Name):
                callee = st.func.id
            elif isinstance(st.func, ast.Attribute) \
                    and isinstance(st.func.value, ast.Name):
                callee = st.func.attr
            else:
                continue
            if callee not in fns:
                continue
            params = fns[callee]
            for idx, arg in enumerate(st.args):
                if isinstance(arg, ast.Name) and arg.id in tainted \
                        and idx < len(params):
                    # 调用方污点 → 被调形参（跨函数 1 层）
                    for st2 in ast.walk(tree):
                        if isinstance(st2, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                                and st2.name == callee:
                            for s in ast.walk(st2):
                                if isinstance(s, ast.Call):
                                    _fn = ""
                                    if isinstance(s.func, ast.Attribute):
                                        _fn = s.func.attr
                                    elif isinstance(s.func, ast.Name):
                                        _fn = s.func.id
                                    if _fn in ("open", "eval", "system",
                                               "Popen", "execute"):
                                        arg_names = {
                                            a.id for a in s.args
                                            if isinstance(a, ast.Name)}
                                        if params[idx] in arg_names:
                                            taint_results.append({
                                                "file": str(path),
                                                "line": s.lineno, "col": 0,
                                                "rule": "taint_flow",
                                                "severity": "warning",
                                                "msg": (f"污点流（跨函数）："
                                                        f"{callee} 形参 "
                                                        f"{params[idx]} 由调用方"
                                                        f"污点变量 {arg.id} 传入 "
                                                        f"→ {_fn}()——输入不可信"
                                                        f"时注入风险"),
                                                "snippet": (lines[s.lineno - 1]
                                                            .strip()[:80]
                                                            if s.lineno <= len(
                                                                lines) else ""),
                                            })
                                            break
