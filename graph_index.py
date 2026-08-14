#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""graph_index.py — P1a 掌握引擎：tree-sitter 多语言符号图索引（抄 codebase-memory 图思路）。

核心（对应 TOP_TIER_PLAN ①）：
  - tree-sitter 解析（18 语言）→ 提取符号（函数/类/方法/导入）
  - 构建调用图：节点=符号，边=调用/导入/继承/引用
  - SQLite 持久化（nodes + edges 表）
  - 图查询：callers_of（谁调用我）/ callees_of（我调用谁）/ impact（影响面 BFS）/
    hubs（中心性——上帝文件/核心模块检测）

用法：
  gi = GraphIndex(db_path)
  gi.index_directory(repo_root)     # 全库索引
  gi.callers_of("module::func")     # 反向调用链
  gi.impact("path/file.py")         # 影响面（BFS 图遍历）
"""
import json
import os
import sqlite3
import threading

# 语言 → tree-sitter 模块/解析器映射（已安装的 18 语言）
_LANG_PARSERS: dict[str, str] = {
    ".py": "tree_sitter_python", ".rs": "tree_sitter_rust",
    ".c": "tree_sitter_c", ".h": "tree_sitter_c", ".cpp": "tree_sitter_cpp",
    ".cc": "tree_sitter_cpp", ".hpp": "tree_sitter_cpp",
    ".go": "tree_sitter_go", ".java": "tree_sitter_java",
    ".js": "tree_sitter_javascript", ".jsx": "tree_sitter_javascript",
    ".ts": "tree_sitter_typescript", ".tsx": "tree_sitter_typescript",
    ".kt": "tree_sitter_kotlin", ".kts": "tree_sitter_kotlin",
    ".php": "tree_sitter_php", ".rb": "tree_sitter_ruby",
    ".swift": "tree_sitter_swift", ".cs": "tree_sitter_c_sharp",
    ".sh": "tree_sitter_bash", ".bash": "tree_sitter_bash",
    ".sql": "tree_sitter_sql", ".md": "tree_sitter_markdown",
    ".gradle": "tree_sitter_groovy", ".groovy": "tree_sitter_groovy",
}

# 语言 → 函数定义节点类型
_FN_NODE_TYPES = {
    "python": ("function_definition", "class_definition"),
    "rust": ("function_item", "impl_item", "trait_item", "struct_item", "enum_item"),
    "c": ("function_definition",),
    "cpp": ("function_definition",),
    "go": ("function_declaration", "method_declaration", "type_declaration"),
    "java": ("method_declaration", "class_declaration", "interface_declaration"),
    "javascript": ("function_declaration", "method_definition", "class_declaration", "arrow_function"),
    "typescript": ("function_declaration", "method_definition", "class_declaration", "interface_declaration"),
    "kotlin": ("function_declaration", "class_declaration"),
    "php": ("function_definition", "class_declaration"),
    "swift": ("function_declaration", "class_declaration"),
    "csharp": ("method_declaration", "class_declaration"),
    "bash": ("function_definition",),
}

# 语言 → 调用表达式节点类型（被调函数名提取）
_CALL_NODE_TYPES = {
    "python": ("call",),
    "rust": ("call_expression", "method_call"),
    "c": ("call_expression",),
    "cpp": ("call_expression",),
    "go": ("call_expression",),
    "java": ("method_invocation", "object_creation_expression"),
    "javascript": ("call_expression", "new_expression"),
    "typescript": ("call_expression", "new_expression"),
    "kotlin": ("call_expression",),
    "php": ("function_call_expression", "method_call_expression"),
    "swift": ("call_expression",),
    "csharp": ("invocation_expression", "object_creation_expression"),
    "bash": ("command",),
}

_MAX_FILES = 2000       # 单次索引文件上限
_MAX_FILE_SIZE = 1_000_000  # 单文件 1MB 上限


class GraphIndex:
    """tree-sitter 符号图索引（SQLite 持久化）。"""

    def __init__(self, db_path: str):
        self._lock = threading.Lock()
        self._db_path = str(db_path)
        self._parsers: dict[str, object] = {}
        with self._lock, sqlite3.connect(self._db_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS nodes("
                         "id TEXT PRIMARY KEY, file TEXT, kind TEXT, "
                         "name TEXT, line INTEGER, lang TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS edges("
                         "src TEXT, dst TEXT, kind TEXT, "
                         "PRIMARY KEY(src, dst, kind))")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst)")

    # ── 解析器 ─────────────────────────────────────────────
    def _get_parser(self, lang: str):
        """按语言名（python/rust/...）获取 tree-sitter 解析器（懒加载）。"""
        if lang not in self._parsers:
            import importlib
            import tree_sitter as ts
            mod_name = {
                "python": "tree_sitter_python", "rust": "tree_sitter_rust",
                "c": "tree_sitter_c", "cpp": "tree_sitter_cpp",
                "go": "tree_sitter_go", "java": "tree_sitter_java",
                "javascript": "tree_sitter_javascript",
                "typescript": "tree_sitter_typescript",
                "kotlin": "tree_sitter_kotlin", "php": "tree_sitter_php",
                "swift": "tree_sitter_swift", "csharp": "tree_sitter_c_sharp",
                "bash": "tree_sitter_bash", "sql": "tree_sitter_sql",
                "markdown": "tree_sitter_markdown", "groovy": "tree_sitter_groovy",
            }.get(lang)
            if mod_name is None:
                return None
            try:
                mod = importlib.import_module(mod_name)
                # tree-sitter ≥0.26: mod.language() 返回 PyCapsule，需 Language 包装
                self._parsers[lang] = ts.Parser(ts.Language(mod.language()))
            except Exception:
                self._parsers[lang] = None
        return self._parsers.get(lang)

    @staticmethod
    def _lang_of(path: str) -> str:
        suffix = os.path.splitext(path)[1].lower()
        return {
            ".py": "python", ".rs": "rust", ".c": "c", ".h": "c",
            ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp",
            ".go": "go", ".java": "java", ".js": "javascript", ".jsx": "javascript",
            ".ts": "typescript", ".tsx": "typescript",
            ".kt": "kotlin", ".kts": "kotlin", ".php": "php",
            ".swift": "swift", ".cs": "csharp",
            ".sh": "bash", ".bash": "bash", ".sql": "sql", ".md": "markdown",
            ".gradle": "groovy", ".groovy": "groovy",
        }.get(suffix, "")

    # ── 索引 ────────────────────────────────────────────────
    def index_directory(self, root: str, progress=None) -> dict:
        """索引整个目录（递归，跳过 .git/node_modules/target/venv 等）。

        两遍：先全库建节点（跨文件解析需要全库符号表就位），再建边。
        """
        skip_dirs = {".git", "node_modules", "target", "venv", ".venv",
                     "__pycache__", "dist", "build", ".cache", ".idea", ".vscode"}
        files = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]
            for fn in filenames:
                p = os.path.join(dirpath, fn)
                if GraphIndex._lang_of(p) and os.path.getsize(p) <= _MAX_FILE_SIZE:
                    files.append(p)
            if len(files) >= _MAX_FILES:
                break
        stats = {"files": 0, "nodes": 0, "edges": 0, "errors": 0}
        with self._lock, sqlite3.connect(self._db_path) as conn:
            # 第一遍：全部文件建节点（清旧）
            contents: dict[str, str] = {}
            for i, f in enumerate(files):
                if progress and i % 50 == 0:
                    progress(i, len(files), f)
                try:
                    with open(f, encoding="utf-8", errors="ignore") as fh:
                        content = fh.read(_MAX_FILE_SIZE)
                    contents[f] = content
                    n = self._index_nodes(conn, f, content)
                    stats["nodes"] += n
                    stats["files"] += 1
                except Exception:
                    stats["errors"] += 1
            # 第二遍：全部文件建边（此时全库 nodes 已就位，可跨文件解析）
            for f, content in contents.items():
                try:
                    stats["edges"] += self._index_edges(conn, f, content)
                except Exception:
                    stats["errors"] += 1
        return stats

    def _index_nodes(self, conn, path: str, content: str) -> int:
        """第一遍：清旧 + 建符号节点。返回节点数。"""
        rel = os.path.basename(path)
        lang = GraphIndex._lang_of(path)
        parser = self._get_parser(lang)
        if parser is None:
            return 0
        old = conn.execute("SELECT id FROM nodes WHERE file = ?", (path,)).fetchall()
        for (nid,) in old:
            conn.execute("DELETE FROM edges WHERE src = ? OR dst = ?", (nid, nid))
        conn.execute("DELETE FROM nodes WHERE file = ?", (path,))
        tree = parser.parse(content.encode("utf-8"))
        root = tree.root_node
        fn_types = _FN_NODE_TYPES.get(lang, ())
        nodes_added = 0

        def walk(node, depth=0):
            nonlocal nodes_added
            if depth > 200:
                return
            if node.type in fn_types:
                name = _node_name(node, lang)
                if name:
                    nid = f"{path}::{name}"
                    conn.execute(
                        "INSERT OR REPLACE INTO nodes(id, file, kind, name, line, lang) "
                        "VALUES (?,?,?,?,?,?)",
                        (nid, path, node.type, name, node.start_point[0] + 1, lang))
                    nodes_added += 1
            for child in node.children:
                walk(child, depth + 1)

        walk(root)
        return nodes_added

    def _index_edges(self, conn, path: str, content: str) -> int:
        """第二遍：建调用边（跨文件解析）。返回边数。"""
        lang = GraphIndex._lang_of(path)
        parser = self._get_parser(lang)
        if parser is None:
            return 0
        fn_types = _FN_NODE_TYPES.get(lang, ())
        call_types = _CALL_NODE_TYPES.get(lang, ())
        if not call_types:
            return 0
        tree = parser.parse(content.encode("utf-8"))
        root = tree.root_node
        edges_added = 0
        for node in _find_all(root, call_types):
            callee = _callee_name(node, lang)
            if not callee:
                continue
            caller = _enclosing_fn(node, fn_types)
            if caller:
                src = f"{path}::{caller}"
                dst = self._resolve_callee(conn, callee, path, lang)
                if src != dst:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO edges(src, dst, kind) VALUES (?,?,?)",
                        (src, dst, "calls"))
                    edges_added += cur.rowcount
        return edges_added

    def _resolve_callee(self, conn, callee: str, cur_file: str, lang: str) -> str:
        """被调函数名 → 定义节点 id（跨文件解析）。

        优先级：
          1. 本文件同名符号（局部函数/方法）
          2. 全库唯一匹配（跨文件调用：interaction.rs 调 removal.rs 的 remove_module）
          3. 本文件兜底（未解析到定义 → 边指向本文件，标注为外部调用）
        内置函数（str/Ok/Err 等）不在 nodes 表，自然落兜底。
        """
        # 1. 本文件
        local = conn.execute(
            "SELECT id FROM nodes WHERE file = ? AND name = ? LIMIT 1",
            (cur_file, callee)).fetchone()
        if local:
            return local[0]
        # 2. 全库唯一匹配（多文件同名 → 取第一个，保守）
        global_hit = conn.execute(
            "SELECT id FROM nodes WHERE name = ? LIMIT 1", (callee,)).fetchone()
        if global_hit:
            return global_hit[0]
        # 3. 兜底（本文件::名，表示"外部/未解析调用"）
        return f"{cur_file}::{callee}"

    # ── 图查询 ──────────────────────────────────────────────
    def callers_of(self, symbol_id: str, limit: int = 50) -> list[dict]:
        """谁调用我（反向调用链）。symbol_id 如 'path/file.py::func'。"""
        with self._lock, sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT src FROM edges WHERE dst = ? AND kind = 'calls' LIMIT ?",
                (symbol_id, limit)).fetchall()
        out = []
        for (src,) in rows:
            node = self._node_info(src)
            out.append({"caller": src, "file": node.get("file", "") if node else "",
                        "line": node.get("line") if node else None})
        return out

    def callees_of(self, symbol_id: str, limit: int = 50) -> list[dict]:
        """我调用谁（正向调用链）。"""
        with self._lock, sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT dst FROM edges WHERE src = ? AND kind = 'calls' LIMIT ?",
                (symbol_id, limit)).fetchall()
        return [{"callee": dst} for (dst,) in rows]

    def impact(self, file_path: str, depth: int = 3, limit: int = 100) -> list[dict]:
        """影响面：文件内所有符号的被调用者集合（BFS 图遍历，深度≤depth）。

        抄 codebase-memory 的 impact analysis：反向调用链多跳。
        """
        with self._lock, sqlite3.connect(self._db_path) as conn:
            syms = conn.execute(
                "SELECT id FROM nodes WHERE file = ?", (file_path,)).fetchall()
            affected: dict[str, int] = {}
            frontier = [s[0] for s in syms]
            seen = set(frontier)
            for d in range(depth):
                nxt = []
                for fid in frontier:
                    rows = conn.execute(
                        "SELECT src FROM edges WHERE dst = ? AND kind = 'calls'",
                        (fid,)).fetchall()
                    for (src,) in rows:
                        if src not in seen:
                            seen.add(src)
                            affected[src] = d + 1
                            nxt.append(src)
                frontier = nxt
                if not frontier:
                    break
        out = []
        for sid, d in sorted(affected.items(), key=lambda kv: kv[1]):
            node = self._node_info(sid)
            out.append({"symbol": sid, "depth": d,
                        "file": node.get("file", "") if node else "",
                        "name": node.get("name") if node else ""})
            if len(out) >= limit:
                break
        return out

    def hubs(self, top: int = 10) -> list[dict]:
        """中心性：入度最高的库内符号（被最多调用 = 核心模块/上帝函数）。

        只统计 nodes 表中真实存在的符号（过滤内置函数/跨库调用）。
        """
        with self._lock, sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT e.dst, count(*) AS c FROM edges e "
                "JOIN nodes n ON n.id = e.dst "
                "WHERE e.kind = 'calls' GROUP BY e.dst ORDER BY c DESC LIMIT ?",
                (top,)).fetchall()
        out = []
        for dst, c in rows:
            node = self._node_info(dst)
            out.append({"symbol": dst, "callers": c,
                        "file": node.get("file", "") if node else "",
                        "name": node.get("name") if node else ""})
        return out

    # ── 社区发现（2026-08-12：抄 GraphRAG 社区发现思路的轻量版）──
    def communities(self, max_communities: int = 20) -> list[dict]:
        """文件级社区发现（连通分量 + 模块度排序）。

        从调用边构建文件级依赖图，跑连通分量（轻量版社区检测），
        输出每个社区：成员文件 + 社区大小 + 内部边密度（模块内聚度）。
        用途：识别"微服务边界"（高内聚社区 = 独立模块，可安全解耦）。

        GraphRAG 用 Leiden 社区发现做全局问答；这里用连通分量做轻量版，
        对中小代码库足够，零额外依赖。
        """
        with self._lock, sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT src, dst FROM edges WHERE kind = 'calls'").fetchall()
        # 文件级邻接（边两端取文件）
        adj: dict[str, set] = {}
        for src, dst in rows:
            sf = src.split("::", 1)[0]
            df = dst.split("::", 1)[0]
            if sf == df:
                continue
            adj.setdefault(sf, set()).add(df)
            adj.setdefault(df, set()).add(sf)
        # BFS 连通分量
        seen: set[str] = set()
        communities: list[list[str]] = []
        for start in adj:
            if start in seen:
                continue
            comp: list[str] = []
            stack = [start]
            seen.add(start)
            while stack:
                cur = stack.pop()
                comp.append(cur)
                for nb in adj.get(cur, ()):
                    if nb not in seen:
                        seen.add(nb)
                        stack.append(nb)
            communities.append(comp)
        # 排序：大社区优先
        communities.sort(key=len, reverse=True)
        # 模块度（社区内边密度）：有向内部边 / (n*(n-1))（有向图最大可能边）
        out = []
        for comp in communities[:max_communities]:
            comp_set = set(comp)
            internal = sum(1 for src, dst in rows
                           if src.split("::", 1)[0] in comp_set
                           and dst.split("::", 1)[0] in comp_set)
            n = len(comp_set)
            max_edges = n * (n - 1) if n > 1 else 1  # 有向图：每对节点最多 2 条边
            density = round(min(internal / max_edges, 1.0), 3) if max_edges else 0.0
            out.append({"size": n, "density": density,
                        "files": sorted(f.split(os.sep)[-1] for f in comp)[:20]})
        return out

    def search_symbols(self, name: str, limit: int = 50) -> list[dict]:
        """按名字搜符号（模糊）。"""
        with self._lock, sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT id, file, kind, name, line FROM nodes "
                "WHERE name LIKE ? LIMIT ?", (f"%{name}%", limit)).fetchall()
        return [{"id": r[0], "file": r[1], "kind": r[2], "name": r[3], "line": r[4]}
                for r in rows]

    def _node_info(self, symbol_id: str) -> dict | None:
        with self._lock, sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT id, file, kind, name, line FROM nodes WHERE id = ?",
                (symbol_id,)).fetchone()
        if row is None:
            return None
        return {"id": row[0], "file": row[1], "kind": row[2],
                "name": row[3], "line": row[4]}

    def stats(self) -> dict:
        with self._lock, sqlite3.connect(self._db_path) as conn:
            n = conn.execute("SELECT count(*) FROM nodes").fetchone()[0]
            e = conn.execute("SELECT count(*) FROM edges").fetchone()[0]
            langs = conn.execute(
                "SELECT lang, count(*) FROM nodes GROUP BY lang").fetchall()
        return {"nodes": n, "edges": e, "db": self._db_path,
                "langs": {l: c for l, c in langs}}


# ── 辅助 ───────────────────────────────────────────────────
def _find_all(node, node_types: tuple) -> list:
    """DFS 收集所有匹配类型的节点。"""
    out = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in node_types:
            out.append(n)
        stack.extend(n.children)
    return out


def _node_name(node, lang: str) -> str:
    """提取函数/类节点名。"""
    try:
        for child in node.children:
            if child.type in ("identifier", "name", "type_identifier",
                              "field_identifier", "function_name"):
                return child.text.decode("utf-8", "ignore")
            if child.type == "function":
                for gc in child.children:
                    if gc.type == "identifier":
                        return gc.text.decode("utf-8", "ignore")
    except Exception:  # 尽力而为（吞错可追溯）
        pass
    return ""


def _callee_name(call_node, lang: str) -> str:
    """从调用表达式提取被调函数名。"""
    try:
        txt = call_node.text.decode("utf-8", "ignore")
        if lang == "python":
            # func(...) / obj.method(...) → func / method
            name = txt.split("(")[0].strip().split(".")[-1].strip()
            return name
        if lang == "rust":
            # foo(...) / self.bar(...) / Type::method(...)
            head = txt.split("(")[0].strip()
            name = head.split("::")[-1].split(".")[-1].strip()
            return name
        # 通用：第一个标识符段
        import re
        m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", txt)
        return m.group(0) if m else ""
    except Exception:
        return ""


def _enclosing_fn(node, fn_types: tuple) -> str:
    """向上找最近的函数定义节点名。"""
    cur = node.parent
    while cur is not None:
        if cur.type in fn_types:
            return _node_name(cur, "") or ""
        cur = cur.parent
    return ""
