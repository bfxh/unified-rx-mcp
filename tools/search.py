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
        # S90 顺带修：stdin 二进制字节通道——text 模式会做 \n→os.linesep 换行翻译
        # （S90 探针实锤），查询内容需字节保真；输出按 utf-8/replace 手工解码。
        cp = subprocess.run(argv, capture_output=True, timeout=120,
                            input=stdin_data.encode("utf-8"))
    except subprocess.TimeoutExpired:
        raise ValueError("rx-search 超时（120s）")
    tail = (cp.stderr or b"").decode("utf-8", errors="replace").strip()[-300:]
    lines = (cp.stdout or b"").decode("utf-8", errors="replace").strip().splitlines()
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


@tool("code_search", "语义代码检索（BM25 符号加权：中文/英文/标识符 → 文件:行；"
      "hybrid=true 时与 code_semantic 定义级结果做 RRF 融合）", "search",
      {"type": "object",
       "properties": {
           "query": {"type": "string", "description": "自然语言/中文/符号查询"},
           "root": {"type": "string", "description": "代码库根目录（默认当前）"},
           "k": {"type": "integer", "description": "返回条数（默认 10）"},
           "hybrid": {"type": "boolean",
                      "description": "RRF 融合语义路（code_semantic 定义级命中）——两路互补，默认 false"},
       },
       "required": ["query"]})
def code_search(query, root=None, k=10, hybrid=False):
    try:
        root = _fs_resolve(root or os.getcwd())   # S88：默认 cwd 同样钳制
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.isdir(root):
        return {"error": f"不是目录: {root}"}
    if not hybrid:
        return _rx_search_call(root, query, k)
    # S101：RRF 融合——两路各取更深候选（k*3 且 ≥20），按 (file,line) 去重后融合
    k_inner = max(int(k) * 3, 20)
    bm25 = _rx_search_call(root, query, k_inner)
    try:
        sem = _rx_semantic_call(root, query, "search", k_inner)
    except ValueError as e:            # exe 缺失等：显式降级，不静默
        out = dict(bm25)
        out["hybrid"] = False
        out["degraded"] = f"语义路不可用，仅 BM25：{e}"
        return out
    if not isinstance(sem, dict) or sem.get("error"):
        out = dict(bm25)
        out["hybrid"] = False
        out["degraded"] = ("语义路不可用，仅 BM25："
                           + str((sem or {}).get("error") if isinstance(sem, dict) else sem))
        return out
    return _rrf_fuse(query, bm25, sem, int(k))




_SEM_EXE_NAME = "rx-semantic.exe"

# S101：RRF（Reciprocal Rank Fusion）常数——业界默认 k=60，免分数归一化
_RRF_K = 60


def _rrf_fuse(query, bm25, sem, k):
    """两路 RRF 融合：score(doc) = Σ 1/(60 + rank)，doc 身份 = (file, line)。

    字段合并：语义路的 symbol/kind 是定义级信息，优先保留；snippet 两路同形。
    输出保留两路 rank 供解释（bm25_rank / semantic_rank，未入该路为 null）。
    """
    ents = {}
    order = []
    for path_name, res in (("bm25", bm25), ("semantic", sem)):
        for rank, h in enumerate(res.get("hits") or [], start=1):
            key = (h.get("file"), h.get("line"))
            if key not in ents:
                ents[key] = {"rrf": 0.0, "bm25_rank": None, "semantic_rank": None,
                             "hit": {"file": h.get("file"), "line": h.get("line")}}
                order.append(key)
            ent = ents[key]
            ent["rrf"] += 1.0 / (_RRF_K + rank)
            ent[f"{path_name}_rank"] = rank
            if path_name == "semantic":
                # 定义级字段优先：symbol/kind 仅语义路有；snippet 语义路是
                # 定义行，比 BM25 的散行更可读 → 覆盖式写入
                for f in ("symbol", "kind", "snippet"):
                    if h.get(f):
                        ent["hit"][f] = h[f]
            else:
                if h.get("snippet") and not ent["hit"].get("snippet"):
                    ent["hit"]["snippet"] = h["snippet"]
    ranked = sorted(order, key=lambda key: -ents[key]["rrf"])[:k]
    hits = []
    for key in ranked:
        ent = ents[key]
        h = dict(ent["hit"])
        h["rrf"] = round(ent["rrf"], 6)
        h["bm25_rank"] = ent["bm25_rank"]
        h["semantic_rank"] = ent["semantic_rank"]
        hits.append(h)
    return {"query": query, "hybrid": True, "rrf_k": _RRF_K,
            "paths": {"bm25": len(bm25.get("hits") or []),
                      "semantic": len(sem.get("hits") or [])},
            "total": len(hits), "hits": hits}


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
        # S90 顺带修：stdin 二进制字节通道——text 模式会做 \n→os.linesep 换行翻译
        # （S90 探针实锤），查询内容需字节保真；输出按 utf-8/replace 手工解码。
        cp = subprocess.run(argv, capture_output=True, timeout=120,
                            input=stdin_data.encode("utf-8"))
    except subprocess.TimeoutExpired:
        raise ValueError("rx-semantic 超时（120s）")
    tail = (cp.stderr or b"").decode("utf-8", errors="replace").strip()[-300:]
    lines = (cp.stdout or b"").decode("utf-8", errors="replace").strip().splitlines()
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
