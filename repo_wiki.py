#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""repo_wiki.py — 代码库结构文档生成器（抄 Qoder Repo Wiki：自动生成可读结构文档）。

从 graph_index 的符号图 + 目录结构生成 markdown 结构文档：
  - 项目地图（目录树 → 模块 → 入口 → 关键符号）
  - 模块清单（每文件：符号数/函数/类）
  - 核心符号（hubs：被调用最多的 = 核心逻辑）
  - 依赖关系（文件间调用边 → 模块依赖）
  - 健康指标（孤儿符号/自包含度）

用法：
  generate_wiki(root, out_path) -> dict  # 生成 markdown 落盘
  供 server.repo_wiki 工具调用（AI 一次调用"看全"仓库）。
"""
import os
import time
from collections import Counter, defaultdict

try:
    from graph_index import GraphIndex
except ImportError:
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from graph_index import GraphIndex  # noqa: F811


def _module_name(file: str, root: str) -> str:
    """文件 → 模块名（相对根，去掉扩展名，/ 分隔）。"""
    rel = os.path.relpath(file, root)
    name = rel.replace("\\", "/")
    for ext in (".rs", ".py", ".js", ".ts", ".go", ".java", ".c", ".cpp", ".h"):
        if name.endswith(ext):
            name = name[: -len(ext)]
            break
    return name


def generate_wiki(root: str, out_path: str, top: int = 15) -> dict:
    """生成代码库 Wiki markdown 并落盘。返回统计。"""
    _t0 = time.perf_counter()
    if not os.path.isdir(root):
        raise ValueError(f"root 不存在: {root}")
    idx_dir = os.path.join(root, ".unified-rx-index")
    os.makedirs(idx_dir, exist_ok=True)
    db = os.path.join(idx_dir, "graph.db")
    gi = GraphIndex(db)
    cur = gi.stats()
    if cur["nodes"] == 0:
        idx_stats = gi.index_directory(root)
    else:
        idx_stats = cur
    # stats() 与 index_directory() 键集不同：统一取交集安全值
    stats = {"files": idx_stats.get("files", 0),
             "nodes": idx_stats.get("nodes", cur.get("nodes", 0)),
             "edges": idx_stats.get("edges", cur.get("edges", 0)),
             "errors": idx_stats.get("errors", 0)}

    # 1. 文件清单 + 符号统计
    import sqlite3
    conn = sqlite3.connect(db)
    files = [r[0] for r in conn.execute(
        "SELECT DISTINCT file FROM nodes ORDER BY file").fetchall()]
    # 真实文件数（stats 的 files 键在缓存路径下缺失——以 nodes 表为准）
    stats["files"] = len(files)
    file_syms = defaultdict(int)
    for f in files:
        file_syms[f] = conn.execute(
            "SELECT count(*) FROM nodes WHERE file = ?", (f,)).fetchone()[0]
    # 2. 核心符号（hubs）
    hubs = gi.hubs(top=top)
    # 3. 文件依赖（边 src 文件 → dst 文件）
    dep_rows = conn.execute(
        "SELECT src, dst FROM edges WHERE kind = 'calls'").fetchall()
    file_deps: dict[str, Counter] = defaultdict(Counter)
    for src, dst in dep_rows:
        sf = src.split("::", 1)[0]
        df = dst.split("::", 1)[0]
        if sf != df:
            file_deps[sf][df] += 1
    conn.close()

    # 4. 组织 markdown
    L = []
    L.append(f"# {os.path.basename(root)} — 代码库 Wiki\n")
    L.append(f"> 由 unified-rx repo_wiki 自动生成（tree-sitter 符号图）\n")
    L.append(f"> 文件 {stats['files']} | 符号 {stats['nodes']} | 调用边 {stats['edges']} | 错误 {stats['errors']}\n")
    L.append("\n## 模块地图\n")
    L.append("| 模块 | 符号数 | 说明 |")
    L.append("|---|---|---|")
    for f in sorted(files, key=lambda x: -file_syms[x]):
        m = _module_name(f, root)
        L.append(f"| `{m}` | {file_syms[f]} | 依赖 {len(file_deps[f])} 个模块 |")
    L.append("\n## 核心符号（被调用最多）\n")
    L.append("| 符号 | 被调用 | 位置 |")
    L.append("|---|---|---|")
    for h in hubs:
        loc = os.path.relpath(h.get("file", ""), root).replace("\\", "/") if h.get("file") else ""
        L.append(f"| `{h['name']}` | {h['callers']} | `{loc}` |")
    L.append("\n## 模块依赖（Top 调用关系）\n")
    L.append("| 调用方 | 被调用方 | 次数 |")
    L.append("|---|---|---|")
    all_deps = []
    for sf, deps in file_deps.items():
        for df, c in deps.most_common():
            all_deps.append((sf, df, c))
    all_deps.sort(key=lambda x: -x[2])
    for sf, df, c in all_deps[:25]:
        L.append(f"| `{_module_name(sf, root)}` → `{_module_name(df, root)}` | {c} |")
    L.append("\n## 说明\n")
    L.append("- 依赖 = 该文件内符号被调用/调用其他文件的边（跨文件）\n")
    L.append("- 重新生成：`repo_wiki(root)` 覆盖 `{root}/.unified-rx-index/WIKI.md`\n")

    md = "\n".join(L)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(md)
    # IDE 增强 217：依赖密集模块（被依赖边最多的文件——改动影响面最大）
    _dep_cnt: dict[str, int] = {}
    for _sf, _deps in file_deps.items():
        for _df, _c in _deps.items():
            _dep_cnt[str(_df)] = _dep_cnt.get(str(_df), 0) + _c
    _most_dep = ""
    if _dep_cnt:
        _md_f, _md_c = max(_dep_cnt.items(), key=lambda kv: kv[1])
        _most_dep = f"{os.path.basename(_md_f)}（{_md_c} 处依赖）"
    # IDE 增强 289：符号图语言分布（files 后缀——代码库语言组成一眼可见）
    _wlangs: dict[str, int] = {}
    for _f in files:
        _sfx = os.path.splitext(_f)[1].lower().lstrip(".")
        if _sfx:
            _wlangs[_sfx] = _wlangs.get(_sfx, 0) + 1
    return {"ok": True, "root": root, "wiki": out_path,
            "chars": len(md), "modules": len(files),
            "languages": dict(sorted(_wlangs.items(), key=lambda kv: -kv[1])),
            "hubs": [h["name"] for h in hubs][:10], "index": stats,
            # IDE 增强 196：生成建议（WIKI 可读入口 + 核心模块提示）
            "advice": (f"WIKI 已生成（{len(md)} 字符，{len(files)} 模块）——"
                       f"核心符号 {len(hubs)} 个（首个 "
                       f"{hubs[0]['name'] if hubs else '无'}），改动前先看调用方"
                       if md else "WIKI 生成失败"),
            # IDE 增强 217：依赖密集模块（edges 最多——改动影响面最大）
            "most_depended": _most_dep,
            # IDE 增强 233：生成耗时（ms——性能可见收官）
            "elapsed_ms": round((time.perf_counter() - _t0) * 1000, 1)}
