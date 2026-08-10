#!/usr/bin/env python3
"""代码库认知层（codebase awareness）——不是孤立工具，而是让工具真正知道：
1. 代码库全貌（文件树/符号/哈希持久化索引，增量更新）
2. 你在干嘛（变更感知：本次索引与上次的差异 = 最近改动）
3. 全库扫描（bug/UI 规则跑全库，变更优先排序）

纯 stdlib 零依赖。索引 JSON 存 <root>/.unified-rx-index/index.json。
"""

import hashlib
import json
import os
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
    ".js": re.compile(r"^(?:export\s+)?(?:function|class)\s+(\w+)", re.M),
    ".gd": re.compile(r"^(?:func|class_name)\s+(\w+)", re.M),
}

# 排除目录
_SKIP_DIRS = {".git", "node_modules", "target", "dist", "build", ".pytest_cache",
              "__pycache__", ".idea", ".vscode", "vendor", ".unified-rx-index", ".codebase-memory"}


def _file_hash(src: str) -> str:
    return hashlib.md5(src.encode("utf-8", errors="replace")).hexdigest()[:16]


def _extract_symbols(src: str, suffix: str) -> list[str]:
    pat = _SYMBOL_PATTERNS.get(suffix)
    if not pat:
        return []
    syms = []
    for m in pat.finditer(src):
        syms.append(m.group(1) or m.group(2))
    return sorted(set(syms))[:200]


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
            rel = os.path.relpath(fp, root)
            h = _file_hash(src)
            try:
                mtime = os.path.getmtime(fp)
            except OSError:
                continue  # TOCTOU：read 后文件被删（review should-fix）
            files[rel] = {
                "hash": h,
                "size": size,
                "symbols": _extract_symbols(src, suffix)[:100],
                "mtime": mtime,
            }
            total_files += 1
            if total_files >= MAX_FILES:
                break
        if total_files >= MAX_FILES:
            break

    # 变更感知（首次索引无基线 → changed 空）
    is_first = not old
    if is_first:
        changed, added, removed = [], [], []
    else:
        changed = sorted(rel for rel, info in files.items() if old.get(rel, {}).get("hash") != info["hash"])
        added = sorted(rel for rel in files if rel not in old)
        removed = sorted(rel for rel in old if rel not in files)

    data = {
        "root": root,
        "indexed_at": time.time(),
        "file_count": len(files),
        "files": files,
    }
    # 原子写（临时文件 + os.replace，崩溃/磁盘满不留截断文件，review should-fix）
    tmp_path = index_path + ".tmp"
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
    # 按目录聚合
    dirs = {}
    for rel, info in files.items():
        d = os.path.dirname(rel) or "."
        dirs.setdefault(d, []).append({"file": os.path.basename(rel), "symbols": info.get("symbols", [])[:10]})
    top_dirs = sorted(dirs.items(), key=lambda kv: -len(kv[1]))[:20]
    return {
        "ok": True,
        "indexed": True,
        "root": data.get("root"),
        "indexed_at": data.get("indexed_at"),
        "file_count": len(files),
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
    _dir = os.path.dirname(os.path.abspath(__file__))
    if _dir not in os.sys.path:
        os.sys.path.insert(0, _dir)
    from ui_check_core import scan_ui_source

    issues = []
    scanned = 0
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
    }
