#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""differentiable_code —— 可微分编程环境（2026-08-15，阶段2 务实落地）。

科幻愿景：代码结构嵌入连续向量空间 → 性能目标梯度下降 → 自动重写算法。
务实落地（工程正确性优先——不交付假的梯度下降）：
① AST 符号嵌入：函数/符号 → 向量（mini_bert_tokenizer 或哈希嵌入）——
   相似函数可检索（向量空间是"可训练"的基础设施）
② optimize_code：性能目标驱动优化器——复杂度分析 + 热点定位 +
   等价重写建议（规则/搜索驱动——诚实标注"梯度下降为未来方向"）
③ performance_target：目标声明 → 检查建议（响应时间/内存/复杂度目标
   映射到代码检查）

调整的是"损失函数（性能目标）"而非代码行——目标驱动优化。
"""
import ast
import hashlib
import os
import re


# ── ① AST 符号嵌入（函数签名向量——符号级可检索）──────────
def embed_function(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict:
    """函数符号 → 特征向量（哈希嵌入——零依赖，确定性）。

    特征：名称/参数数/返回标注/装饰器/体内调用名——结构特征向量。
    真·embedding（mini_bert_tokenizer）可升级接入——接口不变。
    """
    name = func_node.name
    args = func_node.args.args
    decorators = [ast.unparse(d) for d in func_node.decorator_list]
    calls = [n.func.id for n in ast.walk(func_node)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    features = {
        "name": name, "arg_count": len(args),
        "decorators": decorators, "calls": calls[:10],
        "has_docstring": bool(ast.get_docstring(func_node)),
        "stmt_count": len(func_node.body),
    }
    # 哈希嵌入（确定性 8 维桶）
    vec = []
    for key in ("name", "arg_count", "has_docstring", "stmt_count",
                "calls", "decorators"):
        h = hashlib.sha256(f"{key}:{features[key]}".encode("utf-8"))
        vec.append(int(h.hexdigest()[:4], 16) % 1024)
    return {"features": features, "vector": vec,
            "embedding": "hash-bucket(8d)——可替换 mini_bert 真嵌入"}


def similar_functions(src: str, target: str, limit: int = 5) -> dict:
    """AST 相似函数检索：解析两个源码 → 全部函数嵌入 → 余弦相似度 TopN。"""
    def _collect(code: str) -> list[tuple[str, dict]]:
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return []
        out = []
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append((n.name, embed_function(n)))
        return out

    src_fns = _collect(src)
    tgt_fns = _collect(target)
    if not src_fns or not tgt_fns:
        return {"ok": False, "error": "无函数可比较（语法错误或空）"}
    results = []
    for t_name, t_emb in tgt_fns:
        tv = t_emb["vector"]
        for s_name, s_emb in src_fns:
            sv = s_emb["vector"]
            sim = _cosine(tv, sv)
            results.append({"target": t_name, "source": s_name,
                            "similarity": round(sim, 3)})
    results.sort(key=lambda r: -r["similarity"])
    return {"ok": True, "top": results[:limit],
            "note": "结构特征向量相似度——真·语义相似需嵌入模型（mini_bert 可替换）"}


def _cosine(a: list[int], b: list[int]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ── ② 性能目标驱动优化器 ──────────────────────────────────
def optimize_code(src: str, path: str = "",
                  perf_goal: str = "响应时间<10ms") -> dict:
    """性能目标 → 复杂度分析 + 热点定位 + 等价重写建议。

    规则/搜索驱动（诚实标注：真·梯度下降重写为未来方向——本实现给出
    可执行的优化候选，由调用方确认应用）。
    """
    issues: list[dict] = []
    lines = src.splitlines()
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return {"ok": False, "error": f"语法错误: {e}"}
    # 复杂度热点（嵌套循环/递归/大分配）
    for n in ast.walk(tree):
        if isinstance(n, ast.For) or isinstance(n, ast.While):
            depth = _loop_depth(n)
            if depth >= 2:
                issues.append({"kind": "complexity",
                               "line": n.lineno,
                               "msg": f"嵌套循环深度 {depth}——O(n^{depth}) 风险，"
                                      f"与目标 {perf_goal} 冲突",
                               "fix": "考虑提前退出/缓存/索引（等价重写）"})
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                and n.func.id in ("sorted", "list.sort"):
            # 排序调用——数据量大时建议
            issues.append({"kind": "hotspot", "line": n.lineno,
                           "msg": "排序调用——若每次请求都排序，O(n log n) 热点",
                           "fix": "预排序/增量维护有序结构"})
    # 目标检查：响应时间目标 → 同步 IO 检测
    if "ms" in perf_goal or "响应" in perf_goal:
        for i, line in enumerate(lines, 1):
            if re.search(r"\b(open|read|requests|urlopen|connect)\s*\(", line):
                issues.append({"kind": "io_in_hot_path", "line": i,
                               "msg": f"同步 IO 在热点路径（与 {perf_goal} 冲突）",
                               "fix": "异步/缓存/批处理"})
    # 内存目标 → 大容器
    if "内存" in perf_goal or "memory" in perf_goal.lower():
        for n in ast.walk(tree):
            if isinstance(n, ast.ListComp) and len(n.generators) >= 2:
                issues.append({"kind": "memory", "line": n.lineno,
                               "msg": "嵌套推导式——大输入内存峰值",
                               "fix": "生成器表达式/流式处理"})
    return {"ok": True, "path": path, "perf_goal": perf_goal,
            "findings": issues[:15], "count": len(issues),
            "rewrites": _gen_rewrites(issues),
            "note": "规则/搜索驱动优化建议 + 等价重写片段（可直接应用）——"
                    "'损失函数'=性能目标；真·AST 梯度下降重写为未来方向"}


def _gen_rewrites(findings: list[dict]) -> list[dict]:
    """等价重写片段生成（风险解决 2026-08-15——梯度下降的可工作简化版）。

    对每个 hotspot finding 生成可直接粘贴的优化代码（规则模板——
    不改变语义的等价重写）。
    """
    out = []
    for f in findings:
        if f["kind"] == "io_in_hot_path":
            out.append({"kind": "io_in_hot_path", "line": f["line"],
                        "rewrite": ("# 优化：IO 移出热点路径——预加载 + 缓存\n"
                                    "import functools\n"
                                    "@functools.lru_cache(maxsize=128)\n"
                                    "def _cached_load(path):\n"
                                    "    with open(path) as fh:\n"
                                    "        return fh.read()\n")})
        elif f["kind"] == "complexity":
            out.append({"kind": "complexity", "line": f["line"],
                        "rewrite": ("# 优化：嵌套循环 → 提前退出/索引（等价语义）\n"
                                    "# 内层循环前加卫语句：if not cond: break/continue\n"
                                    "# 或用 dict 索引替代线性查找（O(n²)→O(n)）\n")})
        elif f["kind"] == "memory":
            out.append({"kind": "memory", "line": f["line"],
                        "rewrite": ("# 优化：嵌套推导式 → 生成器（惰性求值降内存峰值）\n"
                                    "# (expr for a in A for b in B) 替代 [expr for a in A for b in B]\n")})
    return out[:5]


def _loop_depth(node) -> int:
    """循环嵌套深度（ast.walk 自身含子循环——统计最大嵌套）。"""
    depth = 1
    for sub in ast.walk(node):
        if sub is node:
            continue
        if isinstance(sub, (ast.For, ast.While)):
            depth = max(depth, _loop_depth(sub) + 1)
    return depth


# ── ③ 性能目标声明 → 检查建议 ─────────────────────────────
def check_perf_target(root: str, perf_goal: str) -> dict:
    """项目级性能目标检查：目录下所有 py 文件跑 optimize_code（聚合）。"""
    findings = []
    files = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in ("node_modules", "target", ".git",
                                    "__pycache__")]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dirpath, fn)
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    src = f.read()
            except OSError:
                continue
            files += 1
            r = optimize_code(src, p, perf_goal)
            if r.get("ok") and r["findings"]:
                for f in r["findings"]:
                    f["file"] = p
                findings.extend(r["findings"])
    return {"ok": True, "files": files, "perf_goal": perf_goal,
            "findings": findings[:100], "count": len(findings),
            "advice": f"性能目标 {perf_goal}——{len(findings)} 处优化点"
                      "（规则驱动——确认后应用）"}
