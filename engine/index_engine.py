from __future__ import annotations
import sys as _sys
for _m in ['cb_index_core', 'graph_index', 'search_core', 'search_index']:
    _sys.modules.setdefault(_m, _sys.modules[__name__])

"""index_engine — 索引引擎。
新技术 = 往本模块增量加函数（不新建零散文件）。
"""


# ══════════════ cb_index_core（合并） ══════════════
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""代码库认知层（codebase awareness）——不是孤立工具，而是让工具真正知道：
1. 代码库全貌（文件树/符号/哈希持久化索引，增量更新）
2. 你在干嘛（变更感知：本次索引与上次的差异 = 最近改动）
3. 全库扫描（bug/UI 规则跑全库，变更优先排序）

纯 stdlib 零依赖。索引 JSON 存 <root>/.unified-rx-index/index.json。
"""

import hashlib
import json
import os

# 引擎根（合并后 __file__ 在 engine/ 下——数据文件在仓库根）
_ENGINE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import re
import time
from pathlib import Path

INDEX_DIR = ".unified-rx-index"
MAX_FILES = 5000
MAX_FILE_SIZE = 1 << 20  # 单文件 1MB

# 语言 → 符号提取正则（文件级快速提取，够用即可）
_SYMBOL_PATTERNS = {
    ".py": re.compile(r"^(?:async\s+)?def\s+(\w+)|^class\s+(\w+)", re.M),
    ".rs": re.compile(r"^(?:pub\s+)?(?:async\s+)?fn\s+(\w+)|^(?:pub\s+)?(?:struct|enum|trait|impl)\s+(\w+)", re.M),
    ".go": re.compile(r"^func\s+(\w+)", re.M),
    ".ts": re.compile(r"^(?:export\s+)?(?:function|class|interface|type|const|let)\s+(\w+)", re.M),
    ".tsx": re.compile(r"^(?:export\s+)?(?:function|class|interface|type|const|let)\s+(\w+)", re.M),
    ".js": re.compile(r"^(?:export\s+)?(?:function|class)\s+(\w+)", re.M),
    ".jsx": re.compile(r"^(?:export\s+)?(?:function|class)\s+(\w+)", re.M),
    ".gd": re.compile(r"^(?:func|class_name)\s+(\w+)", re.M),
    # IDE 增强 255：c/cpp 符号（函数声明/struct/typedef——graph_index 已支持，
    # cb_index 对齐；文本启发防多指针/宏误抓）
    ".c": re.compile(r"^(?:static\s+|inline\s+|extern\s+)*[A-Za-z_]\w*\s+"
                     r"([A-Za-z_]\w*)\s*\([^)]*\)\s*(?:\{)?|"
                     r"^(?:typedef\s+)?(?:struct|enum|union)\s+(\w+)", re.M),
    ".h": re.compile(r"^(?:static\s+|inline\s+|extern\s+)*[A-Za-z_]\w*\s+"
                     r"([A-Za-z_]\w*)\s*\([^)]*\)\s*(?:\{)?|"
                     r"^(?:typedef\s+)?(?:struct|enum|union)\s+(\w+)", re.M),
    ".cpp": re.compile(r"^(?:static\s+|inline\s+|virtual\s+|explicit\s+)*"
                       r"[A-Za-z_:]\w*\s+([A-Za-z_]\w*)\s*\([^)]*\)\s*(?:\{)?|"
                       r"^class\s+(\w+)|^(?:struct|enum)\s+(\w+)", re.M),
    ".hpp": re.compile(r"^(?:static\s+|inline\s+|virtual\s+|explicit\s+)*"
                       r"[A-Za-z_:]\w*\s+([A-Za-z_]\w*)\s*\([^)]*\)\s*(?:\{)?|"
                       r"^class\s+(\w+)|^(?:struct|enum)\s+(\w+)", re.M),
    # IDE 增强 266：cs/lua/sh 符号（C# class/方法、Lua function、Bash 函数）
    ".cs": re.compile(r"^(?:public\s+|private\s+|internal\s+|protected\s+)*"
                      r"(?:static\s+|virtual\s+|override\s+|async\s+)*"
                      r"(?:class|interface|struct|enum)\s+(\w+)|"
                      r"^(?:public\s+|private\s+|internal\s+|protected\s+)*"
                      r"(?:static\s+|virtual\s+|override\s+|async\s+)*"
                      r"[A-Za-z_<>,\[\]\s]*\s+([A-Za-z_]\w*)\s*\([^)]*\)\s*\{?", re.M),
    ".lua": re.compile(r"^(?:local\s+)?function\s+(\w+)|^local\s+(\w+)\s*=", re.M),
    ".sh": re.compile(r"^([A-Za-z_]\w*)\s*\(\)\s*(?:\{|$)", re.M),
    ".bash": re.compile(r"^([A-Za-z_]\w*)\s*\(\)\s*(?:\{|$)", re.M),
    # IDE 增强 270：java/kt/swift/php/rb/ps1 符号（对齐 bug_scan 22 语言）
    ".java": re.compile(r"^\s*(?:public\s+|private\s+|protected\s+)*(?:static\s+|final\s+)*"
                        r"(?:class|interface|enum)\s+(\w+)|"
                        r"^\s*(?:public\s+|private\s+|protected\s+)*(?:static\s+|final\s+)*"
                        r"[A-Za-z_<>\[\]]*\s+(\w+)\s*\([^)]*\)\s*(?:\{|$)", re.M),
    ".kt": re.compile(r"^\s*(?:fun\s+)?(\w+)\s*\(|^\s*class\s+(\w+)", re.M),
    ".kts": re.compile(r"^\s*(?:fun\s+)?(\w+)\s*\(|^\s*class\s+(\w+)", re.M),
    ".swift": re.compile(r"^\s*func\s+(\w+)|^\s*class\s+(\w+)|^\s*struct\s+(\w+)", re.M),
    ".php": re.compile(r"^\s*function\s+(\w+)|^\s*class\s+(\w+)", re.M),
    ".rb": re.compile(r"^\s*def\s+(\w+)|^\s*class\s+(\w+)|^\s*module\s+(\w+)", re.M),
    ".ps1": re.compile(r"^\s*function\s+([A-Za-z_][\w-]*)", re.M),
    ".dart": re.compile(r"^(?:class|abstract class|mixin|enum)\s+(\w+)|"
                        r"^\s*(?:Future\s*<[^>]*>\s*|Widget\s+|void\s+|int\s+|String\s+|"
                        r"bool\s+|double\s+|List\s*<[^>]*>\s*|Map\s*<[^>]*>\s*)?"
                        r"(?!TextButton|ElevatedButton|OutlinedButton|IconButton|"
                        r"FilledButton|Column|Row|Container|Text|SizedBox)"
                        r"(\w+)\s*\(", re.M),
}

# 排除目录
_SKIP_DIRS = {".git", "node_modules", "target", "dist", "build", ".pytest_cache",
              "__pycache__", ".idea", ".vscode", "vendor", ".unified-rx-index", ".codebase-memory"}


def _file_hash(src: str) -> str:
    # MD5 仅用于文件内容变更检测（非安全场景）；usedforsecurity=False 显式标记
    # （bandit B324 消除误报；不用于密码/签名/校验和对抗）
    return hashlib.md5(src.encode("utf-8", errors="replace"), usedforsecurity=False).hexdigest()[:16]


def _extract_symbols(src: str, suffix: str) -> dict[str, int]:
    """提取符号 → 定义行号（1-based）。返回 {符号: 行号}，按行号排序。"""
    pat = _SYMBOL_PATTERNS.get(suffix)
    if not pat:
        return {}
    syms: dict[str, int] = {}
    for m in pat.finditer(src):
        sym = m.group(1) or m.group(2)
        if sym:
            syms.setdefault(sym, src.count("\n", 0, m.start()) + 1)
    return dict(list(sorted(syms.items(), key=lambda kv: kv[1]))[:200])


def index_repo(root: str) -> dict:
    """扫描代码库，构建/更新索引。返回 {ok, files, symbols, changed, added, removed, index_path}。

    changed = 相对上次索引内容变化的文件（这就是"你在干嘛"的答案）。
    """
    root = str(Path(root).resolve())
    index_dir = os.path.join(root, INDEX_DIR)
    os.makedirs(index_dir, exist_ok=True)
    index_path = os.path.join(index_dir, "index.json")

    old = {}
    if os.path.exists(index_path):
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                old = loaded.get("files", {})
                if not isinstance(old, dict):
                    old = {}
        except (json.JSONDecodeError, OSError, AttributeError):
            old = {}

    files = {}
    total_files = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in sorted(filenames):
            suffix = os.path.splitext(name)[1].lower()
            if suffix not in _SYMBOL_PATTERNS:
                continue
            fp = os.path.join(dirpath, name)
            try:
                size = os.path.getsize(fp)
                if size > MAX_FILE_SIZE:
                    continue
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    src = f.read()
            except OSError:
                continue
            rel = os.path.relpath(fp, root).replace("\\", "/")  # LOW 修复：统一正斜杠键（Windows 兼容 locate）
            h = _file_hash(src)
            try:
                mtime = os.path.getmtime(fp)
            except OSError:
                continue  # TOCTOU：read 后文件被删（review should-fix）
            files[rel] = {
                "hash": h,
                "size": size,
                "symbols": dict(list(_extract_symbols(src, suffix).items())[:100]),
                "mtime": mtime,
            }
            total_files += 1
            if total_files >= MAX_FILES:
                break
        if total_files >= MAX_FILES:
            break

    # 变更感知（首次索引无基线 → changed 空）
    is_first = not old
    truncated = total_files >= MAX_FILES  # 截断标志（2026-08-14：removed 防误报）
    if is_first:
        changed, added, removed = [], [], []
    else:
        # LOW 修复：兼容反斜杠旧键 + files 元素类型校验
        old = {k.replace("\\", "/"): v for k, v in old.items()}
        old = {k: v for k, v in old.items() if isinstance(v, dict)}
        changed = sorted(rel for rel, info in files.items() if old.get(rel, {}).get("hash") != info["hash"])
        added = sorted(rel for rel in files if rel not in old)
        if truncated:
            # 截断时不报 removed：无法区分"真删除"与"被截断未扫描"——
            # 宁可漏报删除（下次全量索引自然发现）也不误报（误导消费端
            # 认为文件没了）。正确性优先（缓存优化原则：宁可 miss 不可错）。
            removed = []
        else:
            removed = sorted(rel for rel in old if rel not in files)

    data = {
        "root": root,
        "indexed_at": time.time(),
        "file_count": len(files),
        "truncated": truncated,  # IDE 增强 143：持久化截断标志（cb_status 可读）
        "files": files,
    }
    # 原子写（临时文件 + os.replace，崩溃/磁盘满不留截断文件，review should-fix）
    # 唯一 tmp 名（pid）：parallel 并发 cb_index 防共享 tmp 竞态（security review MEDIUM）
    tmp_path = f"{index_path}.tmp.{os.getpid()}"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp_path, index_path)

    return {
        "ok": True,
        "root": root,
        "file_count": len(files),
        "symbol_count": sum(len(v["symbols"]) for v in files.values()),
        "changed": changed,
        "added": added,
        "removed": removed,
        "truncated": truncated,
        "files": files,
        "index_path": index_path,
        "is_first_index": is_first,
    }


def repo_status(root: str) -> dict:
    """读取现有索引状态（不重建）：文件树摘要 + 上次变更记录。"""
    root = str(Path(root).resolve())
    index_path = os.path.join(root, INDEX_DIR, "index.json")
    if not os.path.exists(index_path):
        return {"ok": False, "indexed": False, "msg": "尚未索引（先调用 cb_index）"}
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"ok": False, "indexed": False, "msg": "索引结构损坏（非 dict）"}
    except (json.JSONDecodeError, OSError, AttributeError) as exc:
        return {"ok": False, "indexed": False, "msg": f"索引损坏: {exc}"}
    files = data.get("files", {})
    if not isinstance(files, dict):
        return {"ok": False, "indexed": False, "msg": "索引结构损坏（files 非 dict）"}
    # LOW 修复：兼容旧反斜杠键（Windows relpath）→ 统一正斜杠
    files = {k.replace("\\", "/"): v for k, v in files.items()}
    # 按目录聚合（symbols 兼容新旧格式：新 dict {sym:line} / 旧 list）
    def _sym_names(symbols) -> list[str]:
        if isinstance(symbols, dict):
            return list(symbols.keys())
        return symbols if isinstance(symbols, list) else []
    dirs = {}
    _idx_langs: dict[str, int] = {}
    for rel, info in files.items():
        if not isinstance(info, dict):  # LOW 修复：files 元素类型校验
            continue
        d = os.path.dirname(rel) or "."
        _sfx = os.path.splitext(rel)[1].lower().lstrip(".")
        if _sfx:
            _idx_langs[_sfx] = _idx_langs.get(_sfx, 0) + 1
        dirs.setdefault(d, []).append({"file": os.path.basename(rel), "symbols": _sym_names(info.get("symbols", []))[:10]})
    top_dirs = sorted(dirs.items(), key=lambda kv: -len(kv[1]))[:20]
    # IDE 增强 137：索引新鲜度（可读——多久没更新，建议刷新提示）
    _fresh = ""
    _iat = data.get("indexed_at")
    if _iat:
        try:
            _age = time.time() - float(_iat)
            if _age < 86400:
                _fresh = f"索引距今 {int(_age // 3600)}h{int(_age % 3600 // 60)}m"
            else:
                _fresh = f"索引距今 {int(_age // 86400)} 天（建议 cb_index 刷新）"
        except (TypeError, ValueError):  # 尽力而为
            pass
    return {
        "ok": True,
        "indexed": True,
        "root": data.get("root"),
        "indexed_at": data.get("indexed_at"),
        "freshness": _fresh,
        # IDE 增强 143：截断状态（索引未覆盖全仓——提示刷新/扩容）
        "truncated": bool(data.get("truncated")),
        "file_count": len(files),
        # IDE 增强 288：索引语言画像（索引文件后缀分布——不重建即可知
        # 项目语言组成，对称扫描工具 languages）
        "languages": dict(sorted(_idx_langs.items(), key=lambda kv: -kv[1])),
        "dir_summary": [{"dir": d, "files": len(v), "symbols": sum(len(x["symbols"]) for x in v)}
                        for d, v in top_dirs],
    }


def scan_repo(root: str, max_files: int = 200) -> dict:
    """全库扫描：先增量索引（感知变更），再对变更+全部文件跑 bug_scan/ui_check 规则。

    结果按"变更优先"排序——工具知道你先动过哪里。
    """
    root = str(Path(root).resolve())
    idx = index_repo(root)
    changed = set(idx["changed"]) | set(idx["added"])

    # 懒加载 UI 扫描器（与 server.py 同目录）；bug 扫描在 server 层包装（cb 全库扫描聚焦 UI + 索引）
    _dir = _ENGINE_ROOT
    if _dir not in os.sys.path:
        os.sys.path.insert(0, _dir)
    from ui_check_core import scan_ui_source

    issues = []
    scanned = 0
    _lang_count: dict[str, int] = {}
    files = idx["files"]
    # 变更优先排序：先扫 changed/added，再扫其余
    ordered = sorted(files, key=lambda rel: (rel not in changed, rel))
    for rel in ordered:
        if scanned >= max_files:
            break
        fp = os.path.join(root, rel)
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                src = f.read()
        except OSError:
            continue
        scanned += 1
        is_changed = rel in changed
        _sfx = os.path.splitext(rel)[1].lower().lstrip(".")
        if _sfx:
            _lang_count[_sfx] = _lang_count.get(_sfx, 0) + 1
        if rel.endswith(".gd"):
            # IDE 增强 259：cb_scan 含 Godot UI（对齐 ui_check 257——变更优先
            # UI 扫描对 Bevy + Godot 双引擎生效）
            from ui_check_core import _scan_gd_ui
            for issue in _scan_gd_ui(src, rel):
                issue["file"] = rel
                issue["priority"] = "changed" if is_changed else "full"
                issues.append(issue)
            continue
        if rel.endswith(".cs"):
            # IDE 增强 268：cb_scan 含 Unity UI（对齐 ui_check 267——三引擎
            # 变更优先 UI 扫描）
            from ui_check_core import _scan_cs_ui
            for issue in _scan_cs_ui(src, rel):
                issue["file"] = rel
                issue["priority"] = "changed" if is_changed else "full"
                issues.append(issue)
            continue
        if rel.endswith(".dart"):
            # IDE 增强 274：cb_scan 含 Flutter UI（四引擎变更优先扫描）
            from ui_check_core import _scan_dart_ui
            for issue in _scan_dart_ui(src, rel):
                issue["file"] = rel
                issue["priority"] = "changed" if is_changed else "full"
                issues.append(issue)
            continue
        for issue in scan_ui_source(src, rel, dir_mode=True):
            issue["file"] = rel
            issue["priority"] = "changed" if is_changed else "full"
            issues.append(issue)

    # 排序：变更优先，其次 severity
    sev_rank = {"error": 0, "warning": 1}
    issues.sort(key=lambda i: (i.get("priority") != "changed", sev_rank.get(i.get("severity"), 2), i.get("file"), i.get("line")))
    return {
        "ok": True,
        "root": root,
        "scanned_files": scanned,
        "changed_files": len(changed),
        "issue_count": len(issues),
        "issues": issues,
        # IDE 增强 285：cb_scan 语言画像（UI 文件后缀分布——四引擎一眼可见）
        "languages": dict(sorted(_lang_count.items(), key=lambda kv: -kv[1])),
        # IDE 增强 133：变更优先提示（changed 文件 = 你正在改的——优先排查）
        "advice": (f"{len(changed)} 个文件有改动（优先排查——issues 中 priority=changed 排前）"
                   if changed else "无变更文件——按 severity 排序"),
    }
# ══════════════ graph_index（合并） ══════════════
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
# ══════════════ search_core（合并） ══════════════
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""search_core — 本地语义代码检索桥接（Python → rx-search Rust 常驻子进程）。

对齐 rx-core/telemetry_core 模式：Popen 常驻 + stdin 行协议。
- index(root)：构建/重建索引（root 变化才重建——内存索引驻留）
- search(q, k, root)：语义检索（BM25 + 符号加权）
- status()：索引状态
失败静默（未编译 → 调用方降级）。
环境变量 RX_SEARCH=0 禁用。
"""

import subprocess

_SEARCH_EXE = None
for _cand in (
    os.path.join(_ENGINE_ROOT, "rx-search", "target", "release", "rx-search.exe"),
    os.path.join(_ENGINE_ROOT, "rx-search", "target", "debug", "rx-search.exe"),
    os.path.join(_ENGINE_ROOT, "rx-search", "target", "release", "rx-search"),
    os.path.join(_ENGINE_ROOT, "rx-search", "target", "debug", "rx-search"),
):
    if os.path.exists(_cand):
        _SEARCH_EXE = _cand
        break

_proc = None
_lock = threading.Lock()
_indexed_root = None
_ENABLED = None


def enabled() -> bool:
    global _ENABLED
    if _ENABLED is None:
        _ENABLED = (os.environ.get("RX_SEARCH", "1") != "0"
                    and _SEARCH_EXE is not None)
    return _ENABLED


def _proc_get():
    global _proc
    if not enabled():
        return None
    with _lock:
        if _proc is None or _proc.poll() is not None:
            _proc = subprocess.Popen(
                [_SEARCH_EXE, "serve"], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", bufsize=1)
        return _proc


def _send(cmd: dict, timeout: float = 60.0):
    p = _proc_get()
    if p is None:
        return None
    try:
        with _lock:
            p.stdin.write(json.dumps(cmd, ensure_ascii=False) + "\n")
            p.stdin.flush()
            line = p.stdout.readline()
        if not line:
            return None
        resp = json.loads(line)
        return resp.get("data") if resp.get("ok") else None
    except Exception:  # noqa: BLE001 —— 失败静默（调用方降级）
        return None


def index(root: str, limit: int = 50000) -> dict | None:
    """构建/重建索引（root 变化才重建——常驻内存索引复用）。"""
    global _indexed_root
    if not enabled():
        return None
    if _indexed_root == root:
        st = status()
        if st:
            return st
    d = _send({"cmd": "index", "root": root, "limit": limit}, timeout=120)
    if d is not None:
        _indexed_root = root
    return d


def search(q: str, root: str = "", k: int = 20,
           limit: int = 50000) -> dict | None:
    """语义检索：未索引/root 变化自动先建索引。"""
    if not enabled():
        return None
    if root:
        index(root, limit)
    hits = _send({"cmd": "search", "q": q, "k": k})
    if hits is None:
        return None
    return {"ok": True, "query": q, "hits": hits, "count": len(hits)}


def status() -> dict | None:
    return _send({"cmd": "status"})


def shutdown() -> None:
    global _proc, _indexed_root
    if _proc is not None and _proc.poll() is None:
        try:
            _send({"cmd": "quit"}, timeout=3.0)
        except Exception:  # noqa: BLE001
            pass
        try:
            _proc.wait(timeout=3.0)
        except Exception:  # noqa: BLE001
            pass
    _proc = None
    _indexed_root = None
# ══════════════ search_index（合并） ══════════════
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""search_index.py — P0b 混合检索层（BM25 全文 + 向量接口 + RRF 融合）。

抄：tantivy/meilisearch 的全文索引思路 + BGE 向量接口 + Reciprocal Rank Fusion。
设计：
  - SQLite FTS5 做 BM25 全文索引（零依赖，Python 内置）
  - 向量检索留接口（embed_fn 可注入 BGE/onnxruntime，未配置时自动降级纯 BM25）
  - RRF 融合两路结果（k=60，业界默认）
  - 供 cb_index / kb_query / lesson 检索复用

用法：
  idx = SearchIndex(path_or_dir)
  idx.add_document(id, text, meta)
  idx.search("查询词")  -> [{id, score, meta}]
  idx.search_hybrid("查询", embed_fn=...)  -> RRF 融合
"""

_RRF_K = 60  # RRF 常数（业界默认）


class SearchIndex:
    """SQLite FTS5 全文索引 + 可选向量 + RRF 融合。

    内部：FTS5 虚拟表 + doc_map 映射表（doc_id -> rowid）。
    FTS5 的删除按内容匹配，故用官方 'delete' 命令（需完整原内容）。
    """

    def __init__(self, db_path: str, table: str = "docs"):
        self._lock = threading.Lock()
        self._db_path = str(db_path)
        self._table = table
        self._map_table = f"{table}_map"
        with self._lock, sqlite3.connect(self._db_path) as conn:
            conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING fts5("
                "title, content, meta)"
            )
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {self._map_table}("
                "doc_id TEXT PRIMARY KEY, rowid INTEGER)"
            )

    # ── 写入 ──────────────────────────────────────────────
    def add_document(self, doc_id: str, content: str, title: str = "",
                     meta: dict | None = None) -> None:
        """新增/替换文档。content 为检索文本，meta 为任意元数据(JSON)。"""
        meta_json = json.dumps(meta or {}, ensure_ascii=False)
        with self._lock, sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                f"SELECT rowid FROM {self._map_table} WHERE doc_id = ?",
                (doc_id,),
            ).fetchone()
            if cur is not None:
                # 普通 DELETE by rowid（FTS5 官方支持；'delete' 命令内容匹配不可靠）
                conn.execute(f"DELETE FROM {self._table} WHERE rowid = ?", (cur[0],))
                conn.execute(f"DELETE FROM {self._map_table} WHERE doc_id = ?", (doc_id,))
            cur = conn.execute(
                f"INSERT INTO {self._table}(title, content, meta) VALUES (?,?,?)",
                (title, content, meta_json),
            )
            new_rowid = cur.lastrowid
            conn.execute(
                f"INSERT INTO {self._map_table}(doc_id, rowid) VALUES (?,?)",
                (doc_id, new_rowid),
            )

    def add_many(self, docs: list[dict]) -> None:
        """批量添加：docs = [{id, content, title?, meta?}, ...]"""
        for d in docs:
            self.add_document(d["id"], d["content"], d.get("title", ""), d.get("meta"))

    def delete(self, doc_id: str) -> None:
        with self._lock, sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                f"SELECT rowid FROM {self._map_table} WHERE doc_id = ?",
                (doc_id,),
            ).fetchone()
            if cur is None:
                return
            conn.execute(f"DELETE FROM {self._table} WHERE rowid = ?", (cur[0],))
            conn.execute(f"DELETE FROM {self._map_table} WHERE doc_id = ?", (doc_id,))

    # ── 检索 ──────────────────────────────────────────────
    def search(self, query: str, limit: int = 20) -> list[dict]:
        """BM25 全文检索（FTS5 默认 bm25 排序）。"""
        if not query.strip():
            return []
        with self._lock, sqlite3.connect(self._db_path) as conn:
            try:
                rows = conn.execute(
                    f"SELECT t.rowid, t.title, t.content, t.meta, "
                    f"bm25({self._table}) AS score "
                    f"FROM {self._table} t WHERE {self._table} MATCH ? "
                    f"ORDER BY score LIMIT ?",
                    (self._query_safe(query), limit),
                ).fetchall()
            except sqlite3.OperationalError:
                return []  # 语法错误（特殊字符）降级空结果
        ids = {r[0]: 1 for r in rows}
        out = []
        if rows:
            with self._lock, sqlite3.connect(self._db_path) as conn:
                for rowid, title, content, meta, score in rows:
                    m = conn.execute(
                        f"SELECT doc_id FROM {self._map_table} WHERE rowid = ?",
                        (rowid,),
                    ).fetchone()
                    out.append({
                        "id": m[0] if m else str(rowid),
                        "title": title, "content": content,
                        "meta": json.loads(meta) if meta else {},
                        "bm25_score": float(score),
                    })
        return out

    def search_hybrid(self, query: str, embed_fn=None, limit: int = 20) -> list[dict]:
        """混合检索：BM25 + 向量（若有 embed_fn），RRF 融合。

        embed_fn(text) -> list[float] 或 None（未配置时纯 BM25）。
        向量检索需子类实现 _vector_search；未实现时自动降级纯 BM25。
        """
        bm25_hits = self.search(query, limit=limit * 2)
        vec_hits = self._vector_search(query, embed_fn, limit=limit * 2) if embed_fn else []
        if not vec_hits:
            return bm25_hits[:limit]
        # RRF 融合（k=60）：排名倒数加权，两路结果取并集
        scores: dict[str, float] = {}
        for rank, hit in enumerate(bm25_hits):
            scores[hit["id"]] = scores.get(hit["id"], 0.0) + 1.0 / (_RRF_K + rank)
        for rank, hit in enumerate(vec_hits):
            scores[hit["id"]] = scores.get(hit["id"], 0.0) + 1.0 / (_RRF_K + rank)
        merged = {h["id"]: h for h in bm25_hits + vec_hits}
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        return [merged[i] for i, _ in ranked[:limit]]

    # ── 内部 ──────────────────────────────────────────────
    @staticmethod
    def _query_safe(q: str) -> str:
        """FTS5 查询转义：去掉特殊语法字符，防语法错误/注入。

        FTS5 特殊字符：`-`(排除) `"`(短语) `*`(前缀) `(`/`)`(分组) `OR`/`AND`/`NOT`。
        统一替换为空格（简单可靠，牺牲少量查询语法能力换取零崩溃）。
        """
        out = []
        for t in q.split():
            t = t.replace('"', "").replace("'", "")
            # 连字符是排除语法（rx-core → rx - core 报错），拆成空格
            t = t.replace("-", " ")
            t = t.replace("(", " ").replace(")", " ")
            t = t.replace("*", " ").replace(":", " ")
            for w in t.split():
                if w.upper() in ("OR", "AND", "NOT"):
                    continue  # 逻辑操作符去掉（防注入语义）
                out.append(w)
        return " ".join(out).strip()[:200]

    def _vector_search(self, query, embed_fn, limit):
        """向量检索接口——由子类/外部注入实现（如 onnxruntime BGE）。"""
        return []

    def stats(self) -> dict:
        with self._lock, sqlite3.connect(self._db_path) as conn:
            n = conn.execute(f"SELECT count(*) FROM {self._map_table}").fetchone()[0]
        return {"table": self._table, "docs": n, "db": self._db_path}