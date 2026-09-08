# -*- coding: utf-8 -*-
"""tools/cache.py —— 内容寻址增量缓存（S103，ADVANCES P1）。

只缓存**纯读分析**工具的结果；写/执行类绝不入缓存（白名单 _CACHEABLE）。

- key = sha256(工具名 + 规范化参数 + 输入指纹)；输入指纹 = 对 root 下文件的
  (相对路径, size, mtime_ns) 序列 + **小文件内容哈希**（≤_HASH_MAX 字节）——
  Windows 系统时钟粒度约 15ms，同尺寸的快速改写 mtime 可能不变（S103 测试
  实锤的假命中路径），小文件纳入内容哈希把这条路堵死；大文件退回
  size+mtime（如实边界）。
- 存储：**进程内** LRU（MCP 服务器常驻，会话内重复调用即命中）；按工具条目数
  上限淘汰最旧。不落盘（跨重启不保留——如实边界，落盘版需另立路径纪律条目）。
- 旁路：调用参数加 `__no_cache: true`（传输层参数，schema 校验前剥除），或设
  环境变量 UNIFIED_RX_NO_CACHE=1。
- 诚实边界：①超过 _HASH_MAX 的大文件只按 size+mtime 判定，理论上可漏检同尺寸
  改写；②文件数超 _MAX_FILES 的仓库不缓存（指纹本身太贵）；③缓存只影响延迟
  不影响语义——命中结果与冷跑逐字节一致（测试锁定）；④进程内、不跨重启。
"""
import hashlib
import json
import os
import threading
import time
from collections import OrderedDict

# 工具 → 其"输入根"参数名。仅纯读、且结果完全由文件内容决定者。
_CACHEABLE = {
    "bug_scan": "path",
    "std_check": "path",
    "ui_check": "path",
    "ast_scan": "path",
    "bug_locate": "root",
    "code_search": "root",
    "code_semantic": "root",
    "repo_map": "root",
    "dep_graph": "path",
    "module_stability": "path",
}

_SKIP_DIRS = {".git", "node_modules", "target", "__pycache__", "dist", "build",
              ".unified-rx-index", "backups", ".venv", "venv", ".idea", ".vscode"}
_MAX_FILES = 20000          # 指纹文件数上限（超限不缓存）
_MAX_ENTRIES = 128          # 进程内条目上限（全局，LRU 淘汰）
_HASH_MAX = 256 * 1024      # 超过此大小的文件不读内容，只按 size+mtime 判定
_HASH_BUDGET = 8 * 1024 * 1024   # 内容哈希总字节预算（超限不缓存：指纹本身太贵）
# 指纹只覆盖代码/文本类扩展名（白名单内各工具只读这些；非代码文件不参与，
# 免把大 JSON/图片的哈希成本算进来）
_FP_EXTS = {".py", ".rs", ".go", ".ts", ".tsx", ".js", ".jsx", ".gd", ".cs",
            ".dart", ".lua", ".java", ".kt", ".rb", ".php", ".swift", ".c",
            ".cpp", ".h", ".hpp", ".sh", ".toml", ".yaml", ".yml", ".md",
            ".json", ".txt", ".ini", ".cfg", ".sql", ".vue", ".svelte"}
_LOCK = threading.Lock()
_STORE = OrderedDict()      # key -> {"ts": int, "value": object}
_STATS = {"hits": 0, "misses": 0, "puts": 0, "evictions": 0, "skipped": 0}


def stats():
    return dict(_STATS)


def reset_stats():
    for k in _STATS:
        _STATS[k] = 0


def clear():
    """清空缓存（测试与排障用）。"""
    with _LOCK:
        _STORE.clear()


def cacheable(name):
    return name in _CACHEABLE


def _content_hash(p, size):
    """小文件内容哈希（≤_HASH_MAX）；读取失败返回空串（按 stat 判定兜底）。"""
    if size > _HASH_MAX:
        return b""
    try:
        with open(p, "rb") as fh:
            return hashlib.sha256(fh.read()).digest()
    except OSError:
        return b""


def fingerprint(root):
    """目录/文件指纹；不可用（越界/超限/读失败）返回 None = 不缓存。"""
    try:
        if os.path.isfile(root):
            st = os.stat(root)
            h = hashlib.sha256()
            h.update(f"{os.path.basename(root)}\x00{st.st_size}"
                     f"\x00{st.st_mtime_ns}\x00".encode())
            h.update(_content_hash(root, st.st_size))
            return h.hexdigest()
        if not os.path.isdir(root):
            return None
        h = hashlib.sha256()
        n = 0
        hashed = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for f in sorted(filenames):
                if os.path.splitext(f)[1].lower() not in _FP_EXTS:
                    continue
                p = os.path.join(dirpath, f)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                rel = os.path.relpath(p, root)
                h.update(f"{rel}\x00{st.st_size}\x00{st.st_mtime_ns}\x00".encode("utf-8"))
                if st.st_size <= _HASH_MAX:
                    hashed += st.st_size
                    if hashed > _HASH_BUDGET:
                        return None     # 预算超限：宁可不缓存
                h.update(_content_hash(p, st.st_size))
                h.update(b"\n")
                n += 1
                if n > _MAX_FILES:
                    return None
        return h.hexdigest()
    except OSError:
        return None


def cache_key(name, args, cursor=None):
    """构造缓存键；不缓存（白名单外/指纹不可用/超限）返回 None。

    cursor 是传输层分页参数（registry 已从 args 剥除）——必须显式纳入键，
    否则第 2 页会命中第 1 页的缓存（S103 全量测试实锤）。
    """
    root_arg = _CACHEABLE.get(name)
    if not root_arg:
        return None
    if os.environ.get("UNIFIED_RX_NO_CACHE") == "1":
        return None
    raw_root = args.get(root_arg)
    try:
        from tools.fs import _resolve as _fs_resolve
        root = _fs_resolve(raw_root or os.getcwd())
    except ValueError:
        return None            # 越界：让工具自己去报错，不入缓存
    fp = fingerprint(root)
    if fp is None:
        _STATS["skipped"] += 1
        return None
    canon = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    h = hashlib.sha256(f"{name}\x00{canon}\x00cursor={cursor}\x00{fp}".encode("utf-8"))
    return h.hexdigest()


def get(key):
    with _LOCK:
        ent = _STORE.get(key)
        if ent is None:
            _STATS["misses"] += 1
            return None
        _STORE.move_to_end(key)
    _STATS["hits"] += 1
    return ent["value"]


def put(key, value):
    with _LOCK:
        _STORE[key] = {"ts": int(time.time()), "value": value}
        _STORE.move_to_end(key)
        while len(_STORE) > _MAX_ENTRIES:
            _STORE.popitem(last=False)
            _STATS["evictions"] += 1
    _STATS["puts"] += 1
