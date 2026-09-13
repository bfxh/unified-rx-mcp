# -*- coding: utf-8 -*-
"""tools/ide_callgraph.py —— S125：真调用图（ide 域）

口径（spec/CALLGRAPH.md）：
- 引擎在 Rust 侧 `rx-scan callgraph`（rust/src/nameres.rs 的同一作用域引擎）——
  节点 = def/class 定义（全限定名），边 = 调用点(file:line) → 定义；
  同文件解析直接裁决，跨文件 import 在 stitch 阶段查模块索引；
- 不可解析调用**如实列出**（reason 分类：external / attr_chain / receiver_var /
  var_call / self_attr_missing / re_export / star_import / not_found / expr），
  不猜（与 NAMERES/IDE 域一贯口径一致）；
- `calls = resolved + unresolved + builtin_calls`（stats 自洽，Rust 侧测试锁死）。

本文件只做两件事：薄壳转调 + 查询层（符号消歧、callers/callees 有界遍历、
汇总（top fan-in/out + 环检出））。exe 缺失报清晰错误，不静默降级。
"""
import json
import os
import subprocess

from registry import tool
from tools.fs import _resolve as _fs_resolve

_RX_EXE_NAME = "rx-scan.exe"
# 查询层硬上限：超出置 truncated（防大图把上下文撑爆）
_MAX_EDGES_OUT = 400
_MAX_CANDIDATES = 20
_MAX_CYCLES = 10

ENGINE = "rust:nameres-callgraph"


def _rx_scan_exe():
    """定位 rx-scan.exe：UNIFIED_RX_RS_EXE 覆盖 → cargo 目标目录惯例路径。"""
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


def _rx_callgraph(root, max_files):
    """薄壳转调 rx-scan.exe callgraph；用法级拒绝 raise ValueError。"""
    exe = _rx_scan_exe()
    if not exe:
        raise ValueError("rx-scan.exe 不存在——先在 rust/ 下 cargo build --release "
                         "（或设 UNIFIED_RX_RS_EXE 指向现有 exe）")
    argv = [exe, "callgraph", root, str(max_files)]
    try:
        cp = subprocess.run(argv, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=600)
    except subprocess.TimeoutExpired:
        raise ValueError("rx-scan callgraph 超时（600s）")
    tail = (cp.stderr or "").strip()[-300:]
    lines = (cp.stdout or "").strip().splitlines()
    if not lines:
        raise ValueError(f"rx-scan 无输出（exit={cp.returncode}）: {tail}")
    try:
        out = json.loads(lines[-1])
    except ValueError:
        raise ValueError(f"rx-scan 输出非 JSON: {lines[-1][:200]}")
    if cp.returncode == 2:
        raise ValueError(out.get("error") if isinstance(out, dict) else lines[-1])
    if cp.returncode != 0:
        raise ValueError(f"rx-scan 执行失败（exit={cp.returncode}）: {tail}")
    return out


def _canon(cyc):
    """环的规范形：最小元素开头轮转（去重键）。"""
    m = min(range(len(cyc)), key=lambda i: cyc[i])
    return tuple(cyc[m:] + cyc[:m])


def _cycles(edges, limit=_MAX_CYCLES):
    """有向环检出（迭代 DFS 着色，回边重建路径；规范形去重；确定性序）。"""
    adj = {}
    for e in edges:
        if e.get("caller") and e.get("callee"):
            adj.setdefault(e["caller"], set()).add(e["callee"])
    found, seen = [], set()
    color = {}
    for start in sorted(adj):
        if color.get(start):
            continue
        color[start] = 1
        stack = [(start, iter(sorted(adj.get(start, ()))))]
        path = [start]
        while stack and len(found) < limit:
            node, it = stack[-1]
            pushed = False
            for nxt in it:
                c = color.get(nxt, 0)
                if c == 1:
                    cyc = path[path.index(nxt):]
                    key = _canon(cyc)
                    if key not in seen:
                        seen.add(key)
                        found.append(list(cyc))
                    if len(found) >= limit:
                        break
                elif c == 0:
                    color[nxt] = 1
                    path.append(nxt)
                    stack.append((nxt, iter(sorted(adj.get(nxt, ())))))
                    pushed = True
                    break
            if not pushed:
                color[node] = 2
                stack.pop()
                path.pop()
    return [{"cycle": c, "length": len(c)} for c in found]


def _summary(raw, edges):
    fan_in, fan_out = {}, {}
    for e in edges:
        fan_in[e["callee"]] = fan_in.get(e["callee"], 0) + 1
        if e.get("caller"):
            fan_out[e["caller"]] = fan_out.get(e["caller"], 0) + 1
    top_in = sorted(fan_in.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    top_out = sorted(fan_out.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    stats = dict(raw.get("stats", {}))
    calls = stats.get("calls", 0)
    non_builtin = max(0, calls - stats.get("builtin_calls", 0))
    stats["resolution_rate"] = (round(stats.get("resolved", 0) / non_builtin, 4)
                                if non_builtin else None)
    return {
        "engine": ENGINE,
        "mode": "summary",
        "root": raw.get("root"),
        "files": raw.get("files"),
        "stats": stats,
        "top_fan_in": [{"symbol": k, "callers": v} for k, v in top_in],
        "top_fan_out": [{"symbol": k, "callees": v} for k, v in top_out],
        "cycles": _cycles(edges),
    }


def _bfs(sym, direction, depth, by_caller, by_callee):
    """有界 BFS：返回 (边列表[带 depth], truncated)。

    direction="callers"：沿 rev 边（callee → 谁调它）；"callees" 对称。
    模块级伪节点（caller=""）入边但不继续展开。
    """
    out, seen_edge = [], set()
    frontier, visited = {sym}, {sym}
    truncated = False
    for d in range(1, depth + 1):
        nxt = set()
        for node in sorted(frontier):
            pool = by_callee.get(node, []) if direction == "callers" else by_caller.get(node, [])
            for e in pool:
                key = (e.get("file"), e.get("line"), e.get("caller"), e.get("callee"))
                if key in seen_edge:
                    continue
                seen_edge.add(key)
                if len(out) >= _MAX_EDGES_OUT:
                    truncated = True
                    continue
                out.append({**e, "depth": d})
                hop = e.get("caller") if direction == "callers" else e.get("callee")
                if hop and hop not in visited:
                    nxt.add(hop)
        visited |= nxt
        frontier = nxt
        if not frontier:
            break
    return out, truncated


def _query(raw, nodes, edges, symbol, direction, depth):
    quals = [n["qual"] for n in nodes if n.get("qual")]
    if symbol in quals:
        matches = [symbol]
    else:
        suffix = "." + symbol
        matches = [q for q in quals if q.endswith(suffix)]
    if not matches:
        return {"engine": ENGINE, "mode": "query", "symbol": symbol,
                "error": "not_found",
                "hint": "短名需与定义名完全一致；或先跑汇总模式（不传 symbol）看 top_fan_in/out"}
    if len(matches) > 1:
        return {"engine": ENGINE, "mode": "query", "symbol": symbol,
                "ambiguous": sorted(matches)[:_MAX_CANDIDATES],
                "count": len(matches),
                "hint": "同名多处，传全限定名（模块.类.方法）消歧"}
    sym = matches[0]
    by_caller, by_callee = {}, {}
    for e in edges:
        by_caller.setdefault(e.get("caller"), []).append(e)
        by_callee.setdefault(e.get("callee"), []).append(e)
    res = {"engine": ENGINE, "mode": "query", "symbol": sym,
           "direction": direction, "depth": depth,
           "stats": raw.get("stats"), "truncated": False}
    if direction in ("callers", "both"):
        cs, t = _bfs(sym, "callers", depth, by_caller, by_callee)
        res["callers"], res["counts_callers"], res["truncated"] = cs, len(cs), res["truncated"] or t
    if direction in ("callees", "both"):
        ce, t = _bfs(sym, "callees", depth, by_caller, by_callee)
        res["callees"], res["counts_callees"], res["truncated"] = ce, len(ce), res["truncated"] or t
    return res


@tool("ide_callgraph", "调用图（S125）：符号级调用边（同文件 + 跨文件 import 解析），"
      "callers/callees 有界遍历；汇总模式给 top fan-in/out 与环；不可解析调用如实分类不猜",
      "ide",
      {"type": "object",
       "properties": {
           "root": {"type": "string", "description": "项目根目录"},
           "symbol": {"type": "string",
                      "description": "符号名（全限定 tools.fs._resolve 或短名 _resolve）；缺省 = 汇总模式"},
           "direction": {"type": "string", "enum": ["callers", "callees", "both"],
                         "description": "遍历方向（默认 both）"},
           "depth": {"type": "integer", "description": "遍历深度 1-8（默认 1）"},
           "max_files": {"type": "integer", "description": "文件上限（默认 300）"},
       },
       "required": ["root"]})
def ide_callgraph(root, symbol="", direction="both", depth=1, max_files=300):
    try:
        root = _fs_resolve(root)
    except ValueError as e:
        return {"error": str(e)}
    try:
        depth = max(1, min(int(depth), 8))
    except (TypeError, ValueError):
        depth = 1
    if direction not in ("callers", "callees", "both"):
        direction = "both"
    try:
        raw = _rx_callgraph(root, max_files)
    except ValueError as e:
        return {"error": str(e)}
    if not isinstance(raw, dict) or "error" in raw:
        return raw if isinstance(raw, dict) else {"error": "rx-scan 输出异常"}
    nodes = raw.get("nodes", [])
    edges = raw.get("edges", [])
    if not symbol:
        return _summary(raw, edges)
    return _query(raw, nodes, edges, symbol, direction, depth)
