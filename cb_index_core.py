#!/usr/bin/env python3
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
    for rel, info in files.items():
        if not isinstance(info, dict):  # LOW 修复：files 元素类型校验
            continue
        d = os.path.dirname(rel) or "."
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
