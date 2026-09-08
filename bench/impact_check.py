# -*- coding: utf-8 -*-
"""impact_check.py —— 跨面耦合检查器（S113，workflow.md 原则 8 工具化）。

回答一个问题：**这次改动，还有哪些"看似不关联"的面会被牵动？**
自动列出六类关联面 + 必跑清单，末行输出 JSON（供留档/机器消费）。

六类关联面：
  A 反向依赖：谁 import/引用了改动模块（全仓 grep）
  B 测试面：tests/ 里提到改动模块名或顶层符号的位置
  C 文档面：README/skills/spec 里提到改动模块名或工具名的位置
  D 计数面：tests/ 与文档里的硬编码计数（工具数/组数/版本号）
  E fixture/oracle：tests/fixtures/ 与改动面的关键词命中
  F 门禁断言：tests/ 里断言 schema/形状/计数、且涉及改动工具名的位置

用法：python bench/impact_check.py [file ...]（缺省 = git diff --name-only main...HEAD）
"""
import ast
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

_SKIP_DIRS = {".git", "__pycache__", "node_modules", "target", "dist", "build", ".venv"}
# 通用名不进"符号面"（否则 main/ROOT/HERE 会把测试面刷成噪声）
_GENERIC = {"main", "ROOT", "HERE", "sys", "os", "re", "json", "run", "test",
            "self", "args", "path", "data", "name", "value", "result"}
_MAX_ITEMS = 200


def changed_files(argv):
    if argv:
        return [a.replace("\\", "/") for a in argv]
    try:
        cp = subprocess.run(["git", "diff", "--name-only", "main...HEAD"],
                            cwd=ROOT, capture_output=True, timeout=60, shell=False)
        out = (cp.stdout or b"").decode("utf-8", "replace")
        return [l.strip() for l in out.splitlines() if l.strip()]
    except (OSError, subprocess.SubprocessError):
        return []


def _top_symbols(path):
    """改动 .py 的顶层符号名（函数/类/常量）。"""
    try:
        src = open(path, encoding="utf-8").read()
        tree = ast.parse(src)
    except (OSError, SyntaxError):
        return []
    names = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nm = node.name
        elif isinstance(node, ast.Assign):
            nm = next((t.id for t in node.targets
                       if isinstance(t, ast.Name) and t.id.isupper()), "")
        else:
            continue
        if len(nm) >= 4 and nm not in _GENERIC:
            names.append(nm)
    return names


def _walk(root, exts):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in sorted(filenames):
            if os.path.splitext(fn)[1] in exts:
                yield os.path.join(dirpath, fn)


def _grep(path, patterns):
    """→ [(行号, 行文本)]，任一 pattern 命中即记。"""
    hits = []
    try:
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return hits
    for i, line in enumerate(lines, 1):
        if any(p.search(line) for p in patterns):
            hits.append((i, line.strip()[:160]))
    return hits


def main():
    files = changed_files(sys.argv[1:])
    if not files:
        print("（无改动文件——传参或先制造 diff）")
        return
    modules, symbols = set(), set()
    for f in files:
        base = os.path.basename(f)
        if base.endswith(".py"):
            modules.add(base[:-3])
            symbols.update(_top_symbols(os.path.join(ROOT, f)))

    mod_pats = [re.compile(r"\b" + re.escape(m) + r"\b") for m in sorted(modules)]
    sym_pats = [re.compile(r"\b" + re.escape(s) + r"\b") for s in sorted(symbols)]
    count_pats = [re.compile(r"工具数|tool_count|工具/12|个组合工具|/\d+ 组|"
                             r"assert\s+\d+\s*<=\s*n\s*<=")]
    gate_pats = [re.compile(r"schema|properties|set\(.*properties|== \{[^}]*\}")]

    out = {"files": files, "modules": sorted(modules), "symbols": sorted(symbols),
           "reverse_imports": [], "tests": [], "docs": [], "counts": [],
           "fixtures": [], "gate_asserts": [], "run": []}

    # A 反向依赖
    for p in _walk(ROOT, {".py"}):
        rel = os.path.relpath(p, ROOT).replace("\\", "/")
        if rel in files:
            continue
        for ln, txt in _grep(p, mod_pats):
            if "import" in txt or "tools." in txt:
                out["reverse_imports"].append(f"{rel}:{ln}  {txt}")

    # B 测试面 / F 门禁断言
    tests_dir = os.path.join(ROOT, "tests")
    for p in _walk(tests_dir, {".py"}):
        rel = os.path.relpath(p, ROOT).replace("\\", "/")
        for ln, txt in _grep(p, mod_pats + sym_pats):
            out["tests"].append(f"{rel}:{ln}  {txt}")
            if any(g.search(txt) for g in gate_pats):
                out["gate_asserts"].append(f"{rel}:{ln}  {txt}")

    # C 文档面 / D 计数面
    docs = [os.path.join(ROOT, "README.md")] + \
        list(_walk(os.path.join(ROOT, "skills"), {".md"})) + \
        list(_walk(os.path.join(ROOT, "spec"), {".md"}))
    for p in docs:
        rel = os.path.relpath(p, ROOT).replace("\\", "/")
        for ln, txt in _grep(p, mod_pats + sym_pats):
            out["docs"].append(f"{rel}:{ln}  {txt}")
        for ln, txt in _grep(p, count_pats):
            out["counts"].append(f"{rel}:{ln}  {txt}")
    for p in _walk(tests_dir, {".py"}):
        rel = os.path.relpath(p, ROOT).replace("\\", "/")
        for ln, txt in _grep(p, count_pats):
            out["counts"].append(f"{rel}:{ln}  {txt}")

    # E fixture/oracle
    fx = os.path.join(ROOT, "tests", "fixtures")
    if os.path.isdir(fx):
        for fn in sorted(os.listdir(fx)):
            low = fn.lower()
            if any(m.lower() in low for m in modules) or \
               any(s.lower() in low for s in symbols):
                out["fixtures"].append(fn)

    # 必跑清单
    if out["tests"] or out["gate_asserts"]:
        out["run"].append("python -m pytest -q -o faulthandler_timeout=45")
    if any(f.startswith("rust/") for f in files):
        out["run"].append("cd rust && cargo test")
    if any(f.startswith(("tools/", "registry.py", "server.py")) for f in files):
        out["run"].append("python server.py --selftest")
    out["run"].append("python bench/impact_check.py（本清单）")

    # 输出
    print(f"== 改动面：{len(files)} 文件 ==")
    for f in files:
        print("  ", f)
    for key, title in (("reverse_imports", "A 反向依赖（谁 import 我）"),
                       ("tests", "B 测试面（引用改动模块/符号）"),
                       ("gate_asserts", "F 门禁断言（schema/形状/计数）"),
                       ("docs", "C 文档面（提及改动模块/符号）"),
                       ("counts", "D 计数面（硬编码计数，需随改动核对）"),
                       ("fixtures", "E fixture/oracle 命中")):
        items = out[key]
        print(f"-- {title}：{len(items)} 处")
        for it in items[:15]:
            print("   ", it)
        if len(items) > 15:
            print(f"    …另有 {len(items) - 15} 处")
    print("-- 必跑清单")
    for c in out["run"]:
        print("   ", c)
    # JSON 留档：每面截断到 _MAX_ITEMS（计数另记）
    jout = {k: (v[:_MAX_ITEMS] if isinstance(v, list) else v) for k, v in out.items()}
    jout["truncated"] = {k: len(v) - _MAX_ITEMS for k, v in out.items()
                         if isinstance(v, list) and len(v) > _MAX_ITEMS}
    print(json.dumps(jout, ensure_ascii=False))


if __name__ == "__main__":
    main()
