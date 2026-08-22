#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""dep_graph —— 代码依赖图索引（2026-08-23，用户：代码相关依赖要索引跟踪）。

解析 Python import / Rust use+mod / JS require+import，构建 文件→依赖文件 图：
- nodes: 仓库内文件（排除 node_modules/vendor/dist/.git/target/__pycache__）
- edges: file -> [依赖文件]（仅仓库内；外部包忽略）
- 反向: dependents（谁依赖我）
- 统计: 依赖最多文件 / 被依赖最多文件 / 环形引用提示

用法：
    python dep_graph.py <root>            # 输出 JSON
    python dep_graph.py <root> --dot      # 输出 Graphviz dot（可选）
CLI:  python cli.py deps <path>
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

_EXCLUDE_DIRS = {"node_modules", "vendor", "dist", "build", "out", ".git",
                 "target", "__pycache__", ".venv", "venv", "assets", "public"}
_EXT_PY = {".py"}
_EXT_RS = {".rs"}
_EXT_JS = {".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"}

_PY_IMPORT = re.compile(
    r"^\s*(?:from\s+([.\w]+)\s+import|import\s+([.\w]+))", re.M)
# Python 标准库模块名（3.10+ 提供）——resolve 前过滤，避免 unresolved 刷屏
_STDLIB = frozenset(getattr(sys, "stdlib_module_names", ()))
_RS_USE = re.compile(r"^\s*use\s+([\w:]+)", re.M)
_RS_MOD = re.compile(r"^\s*(?:pub\s+)?mod\s+(\w+)\s*;", re.M)
_JS_IMPORT = re.compile(
    r"""(?:import\s+[^'"]*?from\s*['"]([^'"]+)['"]|require\s*\(\s*['"]([^'"]+)['"]\s*\))""")
_JS_REL = re.compile(r"^\.{1,2}/")


def _walk(root: Path, limit: int):
    """遍历代码文件（排除目录），返回相对路径列表。"""
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in _EXCLUDE_DIRS and not d.startswith(".")]
        for fn in filenames:
            if len(out) >= limit:
                return out
            rel = os.path.relpath(os.path.join(dirpath, fn), root)
            if Path(fn).suffix in (_EXT_PY | _EXT_RS | _EXT_JS):
                out.append(rel.replace("\\", "/"))
    return out


def _parse_py(src: str, rel: str) -> list[str]:
    """Python import → 模块路径（仓库内相对形式；标准库/相对导入跳过）。"""
    mods: list[str] = []
    for m in _PY_IMPORT.finditer(src):
        mod = (m.group(1) or m.group(2) or "").strip()
        if not mod or mod.startswith("."):
            continue  # 相对导入跳过（同目录文件级依赖复杂，保守不解析）
        top = mod.split(".")[0]
        if top in _STDLIB:
            continue  # 标准库不是仓库内依赖
        mods.append(mod.replace(".", "/"))
    return mods


def _parse_rs(src: str, rel: str) -> list[str]:
    """Rust use/mod → 模块路径。"""
    mods: list[str] = []
    for m in _RS_USE.finditer(src):
        path = m.group(1)
        if path.startswith("crate::"):
            mods.append(path[len("crate::"):].replace("::", "/"))
        elif path.startswith("super::") or path.startswith("self::"):
            # 相对路径：按文件位置展开（简化为同目录/父目录模块名）
            base = Path(rel).parent
            parts = path.split("::")
            while parts and parts[0] in ("super", "self"):
                if parts[0] == "super":
                    base = base.parent
                parts = parts[1:]
            if parts:
                mods.append(str(base.joinpath(*parts)).replace("\\", "/"))
    for m in _RS_MOD.finditer(src):
        # 同目录 mod x; → x.rs 或 x/mod.rs
        base = Path(rel).parent
        mods.append(str(base.joinpath(m.group(1))).replace("\\", "/"))
    return mods


def _parse_js(src: str, rel: str) -> list[str]:
    """JS/TS import/require → 相对路径模块。"""
    mods: list[str] = []
    base = Path(rel).parent
    for m in _JS_IMPORT.finditer(src):
        target = (m.group(1) or m.group(2) or "").strip()
        if not target:
            continue
        if _JS_REL.match(target):
            # 相对导入 → 文件（去扩展名匹配）
            p = (base / target).as_posix()
            mods.append(p)
        # 绝对包名（外部依赖）忽略
    return mods


def _resolve(root: Path, mod_path: str, rel: str, suffix: str) -> str | None:
    """模块路径 → 仓库内文件（存在性校验）。"""
    cands = [mod_path]
    if suffix in _EXT_PY:
        cands += [mod_path + ".py", mod_path + "/__init__.py"]
    elif suffix in _EXT_RS:
        cands += [mod_path + ".rs", mod_path + "/mod.rs"]
    else:
        for s in (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs",
                  "/index.js", "/index.ts", "/index.jsx"):
            cands.append(mod_path + s)
    for c in cands:
        if c.endswith("/"):
            continue
        p = root / c
        if p.is_file():
            return c
    # 相对导入可能带扩展名，直接尝试
    p = root / mod_path
    if p.is_file():
        return mod_path
    return None


def build_dep_graph(root: str, max_files: int = 500) -> dict:
    """构建依赖图。返回 {ok, nodes, edges, dependents, stats, cycles}"""
    root_p = Path(root)
    if not root_p.is_dir():
        return {"ok": False, "error": f"目录不存在: {root}"}
    files = _walk(root_p, max_files)
    by_suffix: dict[str, list[str]] = {}
    for f in files:
        s = Path(f).suffix
        by_suffix.setdefault(s, []).append(f)
    edges: dict[str, list[str]] = {}
    dependents: dict[str, list[str]] = {}
    unresolved: list[str] = []
    for f in files:
        s = Path(f).suffix
        try:
            src = (root_p / f).read_text(encoding="utf-8", errors="replace")[:200000]
        except OSError:
            continue
        mods: list[str] = []
        if s in _EXT_PY:
            mods = _parse_py(src, f)
        elif s in _EXT_RS:
            mods = _parse_rs(src, f)
        else:
            mods = _parse_js(src, f)
        deps: list[str] = []
        for m in mods:
            resolved = _resolve(root_p, m, f, s)
            if resolved and resolved != f:
                deps.append(resolved)
                dependents.setdefault(resolved, []).append(f)
            elif resolved is None and not m.startswith(("node_modules", "vendor")):
                unresolved.append(f"{f} -> {m}")
        if deps:
            edges[f] = sorted(set(deps))
    # 统计
    dep_count = {f: len(v) for f, v in edges.items()}
    dep_count_by = {f: len(v) for f, v in dependents.items()}
    top_deps = sorted(dep_count.items(), key=lambda kv: -kv[1])[:10]
    top_dep_by = sorted(dep_count_by.items(), key=lambda kv: -kv[1])[:10]
    # 简单环检测（DFS，限制规模防爆炸）
    cycles: list[list[str]] = []
    visited: set[str] = set()
    stack: list[str] = []

    def _dfs(n: str) -> None:
        if n in stack:
            i = stack.index(n)
            cyc = stack[i:] + [n]
            if len(cyc) <= 8 and cyc not in cycles:
                cycles.append(cyc)
            return
        if n in visited:
            return
        visited.add(n)
        stack.append(n)
        for d in edges.get(n, []):
            _dfs(d)
        stack.pop()

    for n in list(edges)[:300]:
        _dfs(n)
    cycles = cycles[:10]
    return {
        "ok": True,
        "root": root,
        "files": len(files),
        "nodes": sorted(set(edges) | set(dependents)),
        "edge_count": sum(len(v) for v in edges.values()),
        "edges": edges,
        "dependents": dependents,
        "unresolved": unresolved[:30],
        "stats": {
            "top_dependencies": [{"file": f, "count": c} for f, c in top_deps],
            "top_dependents": [{"file": f, "count": c} for f, c in top_dep_by],
        },
        "cycles": cycles,
        "elapsed_ms": 0,
    }


def to_dot(g: dict) -> str:
    lines = ["digraph deps {"]
    for f in g.get("nodes", []):
        lines.append(f'  "{f}" [shape=box];')
    for src, dsts in g.get("edges", {}).items():
        for d in dsts:
            lines.append(f'  "{src}" -> "{d}";')
    lines.append("}")
    return "\n".join(lines)


if __name__ == "__main__":
    import time as _t
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    dot = "--dot" in sys.argv
    t0 = _t.perf_counter()
    g = build_dep_graph(root)
    g["elapsed_ms"] = round((_t.perf_counter() - t0) * 1000, 1)
    if dot:
        print(to_dot(g))
    else:
        print(json.dumps(g, ensure_ascii=False, indent=1))
