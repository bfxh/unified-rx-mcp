# -*- coding: utf-8 -*-
"""tools/search.py —— 语义检索域（2 工具）：code_search / code_semantic

收敛自旧版 code_search(BM25) + explore_code/semantic_search/dep_graph/kb_query；
kb_query 于 S15 移除（同引擎重复面，L3 实战 100+ 会话零调用）。
S80 起 BM25 引擎 Rust 原生化（rx-search.exe，见 rust/src/search.rs）；
S81 起 code_semantic 也 Rust 原生化（rx-semantic.exe，见 rust/src/sem.rs）——
Python 侧只留薄壳转调，exe 缺失报清晰错误不静默降级。
"""
import os
import json
import subprocess

from registry import tool
from tools.fs import _resolve as _fs_resolve   # S88：S73 纪律补全——读路径过沙盒

# 大查询不走 argv：Windows CreateProcess 命令行上限 32767 UTF-16 码元（代理对
# 最坏翻倍），10000 字符留足余量；argv 传 "-" 时 exe 侧改读 stdin 全文。
_QUERY_ARGV_CAP = 10000

_RX_EXE_NAME = "rx-search.exe"


def _rx_search_exe():
    """定位 rx-search.exe：UNIFIED_RX_RS_EXE 覆盖 → cargo 目标目录惯例路径。

    与 tools/fs.py::_rx_fs_exe 同纪律：候选必须是已存在且文件名恰为
    rx-search.exe 的常规文件（argv 固定前缀、list 形式、无 shell，
    env 覆盖不构成任意命令执行面）。
    """
    cand = []
    override = os.environ.get("UNIFIED_RX_RS_EXE")
    if override:
        cand.append(override)
    tmp = os.environ.get("TEMP", r"C:\Temp")
    cand += [os.path.join(tmp, "rx-rs-target", kind, _RX_EXE_NAME)
             for kind in ("release", "debug")]
    for c in cand:
        if os.path.isfile(c) and os.path.basename(c) == _RX_EXE_NAME:
            return c
    return None


def _rx_search_call(root, query, k):
    """薄壳转调 rx-search.exe，返回结果 dict；用法级拒绝 raise ValueError。

    超 _QUERY_ARGV_CAP 的大查询改走 stdin（argv 传 "-"），绕开 Windows 命令行
    上限；stdin 恒接管（空串即 EOF），子进程绝不继承宿主的协议管道。
    """
    exe = _rx_search_exe()
    if not exe:
        raise ValueError("rx-search.exe 不存在——先在 rust/ 下 cargo build --release "
                         "（或设 UNIFIED_RX_RS_EXE 指向现有 exe）")
    argv = [exe, root, query, str(k)]
    stdin_data = ""
    if len(query) > _QUERY_ARGV_CAP:
        argv[2] = "-"
        stdin_data = query
    try:
        cp = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=120, input=stdin_data)
    except subprocess.TimeoutExpired:
        raise ValueError("rx-search 超时（120s）")
    tail = (cp.stderr or "").strip()[-300:]
    lines = (cp.stdout or "").strip().splitlines()
    if not lines:
        raise ValueError(f"rx-search 无输出（exit={cp.returncode}）: {tail}")
    try:
        out = json.loads(lines[-1])
    except ValueError:
        raise ValueError(f"rx-search 输出非 JSON: {lines[-1][:200]}")
    if cp.returncode == 2:
        # 用法级拒绝（缺参数）→ 与 fs 壳同走 ValueError 包络
        raise ValueError(out.get("error") if isinstance(out, dict) else lines[-1])
    if cp.returncode != 0:
        raise ValueError(f"rx-search 执行失败（exit={cp.returncode}）: {tail}")
    return out


@tool("code_search", "语义代码检索（BM25 符号加权：中文/英文/标识符 → 文件:行）", "search",
      {"type": "object",
       "properties": {
           "query": {"type": "string", "description": "自然语言/中文/符号查询"},
           "root": {"type": "string", "description": "代码库根目录（默认当前）"},
           "k": {"type": "integer", "description": "返回条数（默认 10）"},
       },
       "required": ["query"]})
def code_search(query, root=None, k=10):
    try:
        root = _fs_resolve(root or os.getcwd())   # S88：默认 cwd 同样钳制
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.isdir(root):
        return {"error": f"不是目录: {root}"}
    return _rx_search_call(root, query, k)




_SEM_EXE_NAME = "rx-semantic.exe"


def _rx_semantic_exe():
    """定位 rx-semantic.exe：UNIFIED_RX_RS_EXE 覆盖 → cargo 目标目录惯例路径。

    与 _rx_search_exe 同纪律：候选必须是已存在且文件名恰为 rx-semantic.exe
    的常规文件（argv 固定前缀、list 形式、无 shell，env 覆盖不构成任意命令执行面）。
    """
    cand = []
    override = os.environ.get("UNIFIED_RX_RS_EXE")
    if override:
        cand.append(override)
    tmp = os.environ.get("TEMP", r"C:\Temp")
    cand += [os.path.join(tmp, "rx-rs-target", kind, _SEM_EXE_NAME)
             for kind in ("release", "debug")]
    for c in cand:
        if os.path.isfile(c) and os.path.basename(c) == _SEM_EXE_NAME:
            return c
    return None


def _rx_semantic_call(root, query, mode, k):
    """薄壳转调 rx-semantic.exe，返回结果 dict；用法级拒绝 raise ValueError。

    超 _QUERY_ARGV_CAP 的大查询改走 stdin（argv 传 "-"），绕开 Windows 命令行
    上限；stdin 恒接管（空串即 EOF），子进程绝不继承宿主的协议管道。
    """
    exe = _rx_semantic_exe()
    if not exe:
        raise ValueError("rx-semantic.exe 不存在——先在 rust/ 下 cargo build --release "
                         "（或设 UNIFIED_RX_RS_EXE 指向现有 exe）")
    argv = [exe, root, query, mode, str(k)]
    stdin_data = ""
    if len(query) > _QUERY_ARGV_CAP:
        argv[2] = "-"
        stdin_data = query
    try:
        cp = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=120, input=stdin_data)
    except subprocess.TimeoutExpired:
        raise ValueError("rx-semantic 超时（120s）")
    tail = (cp.stderr or "").strip()[-300:]
    lines = (cp.stdout or "").strip().splitlines()
    if not lines:
        raise ValueError(f"rx-semantic 无输出（exit={cp.returncode}）: {tail}")
    try:
        out = json.loads(lines[-1])
    except ValueError:
        raise ValueError(f"rx-semantic 输出非 JSON: {lines[-1][:200]}")
    if cp.returncode == 2:
        # 用法级拒绝（缺参数/mode 非法）→ 与 fs/search 壳同走 ValueError 包络
        raise ValueError(out.get("error") if isinstance(out, dict) else lines[-1])
    if cp.returncode != 0:
        raise ValueError(f"rx-semantic 执行失败（exit={cp.returncode}）: {tail}")
    return out


@tool("code_semantic", "向量空间语义检索：自然语言 → 符号定义（tf-idf 余弦，"
      "mode=search 找定义 / mode=related 找语义相邻符号）", "search",
      {"type": "object",
       "properties": {
           "query": {"type": "string", "description": "自然语言（search）或符号名（related）"},
           "root": {"type": "string", "description": "代码库根目录（默认当前）"},
           "mode": {"type": "string", "enum": ["search", "related"],
                    "description": "search=语义找定义；related=给定符号的语义邻居"},
           "k": {"type": "integer", "description": "返回条数（默认 8）"},
       },
       "required": ["query"]})
def code_semantic(query, root=None, mode="search", k=8):
    try:
        root = _fs_resolve(root or os.getcwd())   # S88：默认 cwd 同样钳制
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.isdir(root):
        return {"error": f"不是目录: {root}"}
    return _rx_semantic_call(root, query, mode, k)
