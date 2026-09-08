# -*- coding: utf-8 -*-
"""S107 名字解析 oracle：与 stdlib `symtable` 对照 + 包络/确定性契约。

口径（spec/NAMERES.md §六）：
- 对照面 = 每个作用域的**绑定名集合**（我们 bindings 的 scope/name vs symtable
  的 is_local 符号）。
- 已知建模差异（均以运行时实测为准，非缺陷）：
  ① 推导式作用域：symtable 把 list/set/dict 推导式目标算进外层作用域；本解析器
     给推导式独立作用域（3.14 实测 `[n for n in ...]` 之后 `n` 是 NameError）。
     对照时把"我们的 `<comp>` 绑定名"从 symtable 侧剔除。
  ② 3.14 的 `__annotate__` 注解辅助作用域不属于用户代码 → 跳过。
"""
import json
import os
import subprocess
import symtable
import sys

import pytest

import tools  # noqa: F401
from tools import scan as scan_mod

pytestmark = pytest.mark.skipif(scan_mod._rx_scan_exe() is None,
                                reason="rx-scan.exe 缺失")


def _resolve(path):
    cp = subprocess.run([scan_mod._rx_scan_exe(), "resolve", str(path)],
                        capture_output=True, timeout=120)
    lines = (cp.stdout or b"").decode("utf-8", "replace").strip().splitlines()
    assert lines, cp.stderr
    return json.loads(lines[-1])


def _our_scopes(out):
    """→ ({scope_path: {names}}, {parent_scope: {comp 目标名}})

    comp 目标在 symtable 里算外层作用域局部；我们的 `<comp>` 绑定带 parent 字段，
    据此**精确**扣除（不能全局扣——同名可能同时是别处真实局部）。
    """
    scopes, comp = {}, {}
    for b in out.get("bindings", []):
        if b["scope"].startswith("<comp"):
            comp.setdefault(b.get("parent") or "module", set()).add(b["name"])
        else:
            scopes.setdefault(b["scope"], set()).add(b["name"])
    return scopes, comp


def _py_scopes(src):
    """symtable → {scope_path: {local names}}（跳过注解辅助表）。"""
    st = symtable.symtable(src, "t", "exec")
    out = {}

    def walk(t, path):
        name = t.get_name()
        if t.get_type() == "annotation" or name.startswith("__annotate__"):
            return
        cur = name if not path else f"{path}.{name}"
        out[cur] = {s.get_name() for s in t.get_symbols() if s.is_local()}
        for c in t.get_children():
            walk(c, cur)

    walk(st, "")
    return out


_ANON = ("<comp>", "<lambda", "genexpr", "listcomp", "setcomp", "dictcomp", "lambda")


def _norm_path(p):
    parts = [x for x in p.split(".") if x]
    if parts and parts[0] == "top":
        parts[0] = "module"
    return ".".join(parts)


def _anon(p):
    return any(x in p for x in _ANON)


def _compare(path, src):
    """返回 (ok, 差异描述)。路径归一：symtable 的 'top.' 前缀 ↔ 我们的 'module'；
    匿名作用域（推导式/lambda/genexpr）两侧都跳过（差异①，见文件头）。"""
    out = _resolve(path)
    assert "error" not in out, out
    ours, comp = _our_scopes(out)
    py = {_norm_path(k): v for k, v in _py_scopes(src).items()}
    # 我们的顶层作用域标签不含模块前缀（"f" / "C.m"）→ 补 "module." 对齐 symtable
    ours = {(_norm_path(k) if k == "module" else "module." + _norm_path(k)): v
            for k, v in ours.items()}
    diffs = []
    for scope in sorted(set(ours) | set(py)):
        if _anon(scope):
            continue
        a = ours.get(scope, set())
        b = py.get(scope, set())
        # 差异①：symtable 把推导式目标算进外层作用域局部 → 按 parent 精确扣除；
        # 但同名若在父作用域也是真实绑定（for/赋值），则不该扣（那本来就是局部）
        for parent, names in comp.items():
            pnorm = "module" if parent == "module" else "module." + _norm_path(parent)
            if scope == pnorm:
                b = b - (names - ours.get(pnorm, set()))
        if a != b:
            diffs.append(f"{scope}: ours-only={sorted(a - b)} py-only={sorted(b - a)}")
    return (not diffs), diffs


CASES = {
    "basic.py": "X = 1\n\n\ndef f(a, b=X):\n    c = a\n    return c + X\n",
    "nested.py": "def outer():\n    v = 1\n\n    def inner():\n        nonlocal v\n        return v\n\n    return inner\n",
    "class.py": "class C:\n    attr = 2\n\n    def m(self):\n        return self.attr\n",
    "imports.py": "import os.path\nfrom collections import defaultdict as dd\n\n\ndef f():\n    import json\n    return dd, json\n",
    "loops.py": "def f(items):\n    for i in items:\n        print(i)\n    with open('x') as fh:\n        print(fh)\n    try:\n        pass\n    except Exception as e:\n        print(e)\n",
    "comp.py": "src = [1]\nout = [n for n in src]\ngen = (m for m in src)\n",
    "global.py": "x = 1\n\n\ndef f():\n    global x\n    x = 2\n    return x\n",
    "match.py": "def f(v):\n    match v:\n        case [a]:\n            return a\n    return 0\n",
}


def test_symtable_binding_agreement(tmp_path):
    for name, src in CASES.items():
        p = tmp_path / name
        p.write_text(src, encoding="utf-8")
        ok, diffs = _compare(p, src)
        assert ok, f"{name} 与 symtable 不一致: {diffs}"


def test_symtable_agreement_on_repo_files():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    files = ["registry.py", "server.py", "urx_tia_plugin.py"] + \
        [f"tools/{n}" for n in sorted(os.listdir(os.path.join(root, "tools")))
         if n.endswith(".py")]
    bad = []
    for rel in files:
        p = os.path.join(root, rel)
        src = open(p, encoding="utf-8").read()
        ok, diffs = _compare(p, src)
        if not ok:
            bad.append((rel, diffs))
    assert not bad, f"{len(bad)} 个文件与 symtable 不一致: {bad[:3]}"


def test_envelope_and_determinism(tmp_path):
    p = tmp_path / "m.py"
    p.write_text("X = 1\nprint(X)\n", encoding="utf-8")
    a = _resolve(p)
    b = _resolve(p)
    assert json.dumps(a, ensure_ascii=False, sort_keys=True) == \
        json.dumps(b, ensure_ascii=False, sort_keys=True), "两次解析必须逐字节一致"
    assert a["stats"]["module"] >= 1 and a["stats"]["builtin"] >= 1, a

    bad = tmp_path / "bad.py"
    bad.write_text("def f(:\n", encoding="utf-8")
    out = _resolve(bad)
    assert "error" in out and "语法错误" in out["error"], out

    missing = tmp_path / "nope.py"
    out2 = _resolve(missing)
    assert "error" in out2 and "读取失败" in out2["error"], out2


def test_comp_visibility_matches_runtime(tmp_path):
    # 运行时实测：推导式变量在外部不可见（NameError）→ 解析为 unresolved
    src = "src = [1]\nout = [n for n in src]\nafter = n\n"
    p = tmp_path / "c.py"
    p.write_text(src, encoding="utf-8")
    out = _resolve(p)
    assert any(u["line"] == 3 and u["name"] == "n" for u in out["unresolved"]), out
