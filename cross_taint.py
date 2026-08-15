"""cross_taint.py — 跨函数污点传播（CPG 概念：调用边传播）。

在 py_taint_scan 的函数内污点结果上做调用边传播：调用方 f 内 tainted 变量
传给被调 g 的形参 i → g 内该形参参与 sink 调用时补报（跨函数 1 层）。
2026-08-15 速度优化：预构建函数名→节点/sink 索引（原实现对每个污点调用点
全树 walk 找被调函数——O(调用点×树)，大文件 75% 耗时）。
"""

import ast


_SINKS = ("open", "eval", "system", "Popen", "execute")


def cross_taint_scan(tree: "ast.AST", path: str, lines: list,
                     taint_results: list) -> None:
    """调用边污点传播：调用方污点实参 → 被调形参 → 被调内 sink。"""
    if taint_results is None:
        return
    # 预构建：函数签名 / 函数节点 / 函数内 sink 调用（各一次全树 walk）
    fns: dict[str, list[str]] = {}
    fn_sinks: dict[str, list] = {}
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # setdefault：与原实现（walk 首个同名定义）一致——重复定义取首定义
        fns.setdefault(n.name, [a.arg for a in n.args.args])
        sinks: list = []
        for s in ast.walk(n):
            if not isinstance(s, ast.Call):
                continue
            _fn = ""
            if isinstance(s.func, ast.Attribute):
                _fn = s.func.attr
            elif isinstance(s.func, ast.Name):
                _fn = s.func.id
            if _fn in _SINKS:
                sinks.append(s)
        fn_sinks.setdefault(n.name, sinks)
    # 每个函数：污点变量 = 形参 + 函数内 source 赋值（args.get/input）
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        tainted = {a.arg for a in fn.args.args}
        for st in ast.walk(fn):
            if isinstance(st, ast.Assign) and isinstance(st.value, ast.Call):
                _v = st.value
                _fnv = _v.func.attr if isinstance(_v.func, ast.Attribute) else (
                    _v.func.id if isinstance(_v.func, ast.Name) else "")
                _objv = getattr(getattr(_v.func, "value", None), "id", "")
                if (_fnv in ("get", "input", "getenv")
                        and _objv in ("", "args", "os", "sys")):
                    for t in st.targets:
                        if isinstance(t, ast.Name):
                            tainted.add(t.id)
        for st in ast.walk(fn):
            if not isinstance(st, ast.Call):
                continue
            if isinstance(st.func, ast.Name):
                callee = st.func.id
            elif (isinstance(st.func, ast.Attribute)
                    and isinstance(st.func.value, ast.Name)):
                callee = st.func.attr
            else:
                continue
            if callee not in fns:
                continue
            params = fns[callee]
            for idx, arg in enumerate(st.args):
                if not (isinstance(arg, ast.Name)
                        and arg.id in tainted
                        and idx < len(params)):
                    continue
                # 调用方污点 → 被调形参（sink 查预构建索引）
                for s in fn_sinks.get(callee, []):
                    _fn = ""
                    if isinstance(s.func, ast.Attribute):
                        _fn = s.func.attr
                    elif isinstance(s.func, ast.Name):
                        _fn = s.func.id
                    arg_names = {a.id for a in s.args
                                 if isinstance(a, ast.Name)}
                    if params[idx] in arg_names:
                        taint_results.append({
                            "file": str(path),
                            "line": s.lineno, "col": 0,
                            "rule": "taint_flow",
                            "severity": "warning",
                            "msg": (f"污点流（跨函数）："
                                    f"{callee} 形参 {params[idx]} 由调用方"
                                    f"污点变量 {arg.id} 传入"
                                    f" → {_fn}()——输入不可信时注入风险"),
                            "snippet": (lines[s.lineno - 1].strip()[:80]
                                        if s.lineno <= len(lines) else ""),
                        })
                        break