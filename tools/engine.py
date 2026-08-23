# -*- coding: utf-8 -*-
"""tools/engine.py —— 开源引擎适配域（2 工具）：engine_status / engine_query

接入策略（单点接开源最强，不自研轮子）：
- 优先 codegraph（Yan Agent 内核，@colbymchenry/codegraph v1.5.0，MIT）：
  `D:\\rj\\AI\\Yan Agent\\resources\\codegraph-runtime`（node.exe + dist/bin/codegraph.js）
- codegraph 可用（项目已 init）→ 用其 CLI 查询（语义级：符号/文档/签名/调用链）
- 不可用 → 降级 v2 自带 code_search（BM25）

codegraph CLI 实测（2026-08-24，VoxelForge）：
  init 3s / 69 文件 → 1571 节点 / 4412 边；query 亚毫秒；中文语义命中 docstring。
"""
import os
import json
import subprocess

from registry import tool

CODEGRAPH_RT = r"D:\rj\AI\Yan Agent\resources\codegraph-runtime"
NODE = os.path.join(CODEGRAPH_RT, "node.exe")
CG_JS = os.path.join(CODEGRAPH_RT, "lib", "dist", "bin", "codegraph.js")
CBM_EXE = r"D:\开发\codebase-memory-mcp.exe"


def _cg_available():
    return os.path.exists(NODE) and os.path.exists(CG_JS)


def _cg_run(args, timeout=60):
    """跑 codegraph CLI，返回 (rc, stdout, stderr)。"""
    r = subprocess.run([NODE, CG_JS] + args, capture_output=True, text=True, timeout=timeout,
                       env={**os.environ, "PYTHONUTF8": "1", "CODEGRAPH_TELEMETRY": "0"},
                       cwd=r"D:\开发")
    return r.returncode, r.stdout, r.stderr


def _probe_codegraph():
    info = {"detected": _cg_available(), "runtime": CODEGRAPH_RT if _cg_available() else None}
    if info["detected"]:
        pj = os.path.join(CODEGRAPH_RT, "package.json")
        if os.path.exists(pj):
            try:
                with open(pj, "r", encoding="utf-8", errors="replace") as f:
                    d = json.load(f)
                info["name"] = d.get("name")
                info["version"] = d.get("version")
            except Exception:
                pass
        kernel = os.path.join(CODEGRAPH_RT, "lib", "kernel", "codegraph-kernel.node")
        if os.path.exists(kernel):
            info["kernel_size_mb"] = round(os.path.getsize(kernel) / 1024 / 1024, 1)
    return info


def _probe_cbm():
    if os.path.exists(CBM_EXE):
        return {"detected": True, "exe": CBM_EXE,
                "size_mb": round(os.path.getsize(CBM_EXE) / 1024 / 1024, 1)}
    return {"detected": False}


@tool("engine_status", "开源引擎接入状态（codegraph/codebase-memory 探测）", "engine",
      {"type": "object", "properties": {}, "required": []})
def engine_status():
    cg = _probe_codegraph()
    # 检查已 init 的项目
    indexed = []
    for proj in [r"D:\开发\VoxelForge", r"D:\开发\VoxelForge-V3", r"D:\开发\unified-rx-v2"]:
        if os.path.exists(os.path.join(proj, ".codegraph")):
            indexed.append(proj)
    cg["indexed_projects"] = indexed
    return {
        "codegraph": cg,
        "codebase_memory": _probe_cbm(),
        "fallback": "v2 自带 code_search（BM25）无条件可用",
        "note": "engine_query 优先 codegraph（语义），不可用自动降级 BM25",
    }


@tool("engine_query", "语义查询：优先 codegraph，降级 BM25", "engine",
      {"type": "object",
       "properties": {
           "query": {"type": "string"},
           "root": {"type": "string", "description": "代码库根目录"},
           "limit": {"type": "integer", "description": "条数（默认 10）"},
       },
       "required": ["query", "root"]})
def engine_query(query, root, limit=10):
    # 1. codegraph 优先
    if _cg_available():
        # 项目需已 init（.codegraph 目录存在）
        has_index = os.path.exists(os.path.join(root, ".codegraph"))
        if has_index:
            try:
                rc, out, err = _cg_run(["query", query, "-p", root, "-l", str(limit), "-j"], 60)
                if rc == 0 and out.strip():
                    try:
                        data = json.loads(out)
                    except json.JSONDecodeError:
                        data = []
                    if data:
                        hits = []
                        for item in data[:limit]:
                            n = item.get("node", {})
                            hits.append({
                                "file": n.get("filePath", ""),
                                "line": n.get("startLine"),
                                "name": n.get("name"),
                                "qualifiedName": n.get("qualifiedName"),
                                "kind": n.get("kind"),
                                "language": n.get("language"),
                                "signature": n.get("signature", ""),
                                "docstring": (n.get("docstring") or "")[:200],
                                "score": round(item.get("score", 0), 2),
                            })
                        return {"engine": "codegraph", "query": query, "total": len(hits),
                                "hits": hits}
                    # 空结果 → 降级（codegraph 无命中但项目已索引）
                # rc != 0 或解析失败 → 降级
            except subprocess.TimeoutExpired:
                pass
    # 2. 降级 BM25
    from .search import code_search
    r = code_search(query, root, limit)
    hits = [{"file": h["file"], "line": h.get("line"), "score": h.get("score"),
             "snippet": h.get("snippet", "")[:200]}
            for h in r.get("hits", [])]
    return {"engine": "bm25_fallback", "query": query, "total": len(hits),
            "hits": hits, "note": "codegraph 无命中/未索引，降级 BM25"}
    from .search import code_search
    r = code_search(query, root, limit)
    return {"engine": "bm25_fallback", "note": "codegraph 不可用/未索引，降级 BM25", **r}
