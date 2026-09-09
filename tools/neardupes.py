# -*- coding: utf-8 -*-
"""tools/neardupes.py —— 近似重复/同族文件聚类（S117）：bottom-k MinHash 指纹 + Jaccard。

用途：重复代码分堆、样本同族归并、大目录里找"几乎一样"的文件。
指纹走 GPU 两遍选择引擎（实测收益见 spec/GPU.md §二）：
- `ngram_bottomk`（直方图定阈值 + 按阈值发射，回传量 O(n)→O(k)）——
  GPU vs CPU 参考 1.7×@8KB … 551×@16MB，交叉点 8KB；
- 全量哈希输出仅 2.8×（输出带宽受限），故只在回退路径使用。
IO（读盘/遍历）仍是 CPU，GPU 只吃逐位置哈希统计。

S118 规模化：两两比较从 O(n²) 全对降为**精确候选剪枝**（倒排索引 + Jaccard 下界，
不丢真对），并把"文件被上限截断"如实标进 `walk_truncated`（此前静默丢尾）。

口径：**近似**——bottom-k MinHash + Jaccard 阈值，不是逐字节 diff；阈值越高越严。
直方图余弦对高熵数据无区分力（随机文件也 0.98），故不用（实测入 spec/GPU.md §二）。
"""
import math
import os

from registry import tool
from tools import gpu
from tools.fs import _resolve as _fs_resolve

_SKIP_DIRS = {".git", "node_modules", "target", "__pycache__", "dist", "build",
              ".venv", "venv", ".pytest_cache"}


def _walk(root, max_files):
    """遍历（跳过 _SKIP_DIRS）→ (文件列表, 是否因上限截断)。

    截断必须如实上报：静默丢尾会让"扫描结果"看起来是全量（S118 修）。
    """
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in sorted(filenames):
            if len(out) >= max_files:
                return out, True
            out.append(os.path.join(dirpath, fn))
    return out, False


def _candidate_pairs(vecs, threshold):
    """精确候选剪枝 → (候选对, 实际共享过指纹的对数)。

    Jaccard ≥ t 的必要条件：I ≥ t(|A|+|B|)/(1+t) ≥ 2tm/(1+t)（m = 全库最小
    指纹长度，对每对都成立的下界）。倒排索引按哈希累计共享数，只把达到下界的
    对交给精确 Jaccard——**不丢真对**，只跳过必然低于阈值的对。指纹极短
    （m→0）时自动退化为全对比较（安全方向）。
    """
    n = len(vecs)
    m = min((len(v) for v in vecs), default=0)
    need = math.ceil(2.0 * threshold * m / (1.0 + threshold)) if m else 0
    if need <= 1:
        return [(i, j) for i in range(n) for j in range(i + 1, n)], n * (n - 1) // 2
    idx = {}
    for i, v in enumerate(vecs):
        for h in v:
            idx.setdefault(h, []).append(i)
    shared = {}
    for lst in idx.values():
        if len(lst) < 2:
            continue
        for a in range(len(lst) - 1):
            ia = lst[a]
            for ib in lst[a + 1:]:
                key = (ia, ib)
                shared[key] = shared.get(key, 0) + 1
    return [p for p, c in shared.items() if c >= need], len(shared)


def _sketch(data, ng, k, engine):
    """bottom-k MinHash 指纹（GPU 两遍选择，CPU 走独立参考实现）。

    口径说明：直方图/余弦对高熵数据无区分力（实测随机文件也 0.98）——
    近重复检测用经典 bottom-k：两文件的 Jaccard 直接估集合相似度。
    """
    mode = gpu.pick_mode("ngram_bottomk_bytes", len(data), engine)
    if mode == "gpu":
        try:
            return gpu.ngram_bottomk_gpu(data, ng, k), "gpu"
        except gpu.GpuError:
            pass
    return gpu.ngram_bottomk_cpu(data, ng, k), "cpu"


@tool("near_dupes", "近似重复/同族文件聚类：bottom-k MinHash 指纹 + Jaccard 聚类"
      "（GPU 两遍选择 1.7×@8KB … 551×@16MB；两两比较走精确候选剪枝，不丢真对）"
      "——重复代码分堆、样本同族归并", "scan",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "目录（沙盒内）"},
           "ng": {"type": "integer", "description": "n-gram 长度（默认 4）"},
           "k": {"type": "integer", "description": "bottom-k 指纹长度（默认 128）"},
           "threshold": {"type": "number", "description": "Jaccard 阈值（默认 0.8）"},
           "max_files": {"type": "integer", "description": "文件上限（默认 100）"},
           "max_file_mb": {"type": "integer", "description": "单文件上限 MB（默认 64）"},
           "engine": {"type": "string", "enum": ["auto", "cpu", "gpu"],
                      "description": "引擎（默认 auto 按实测交叉点）"},
       },
       "required": ["path"]})
def near_dupes(path, ng=4, k=128, threshold=0.8, max_files=100,
               max_file_mb=64, engine="auto"):
    try:
        path = _fs_resolve(path)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.isdir(path):
        return {"error": f"不是目录: {path}"}
    ng = max(2, min(16, int(ng)))
    k = max(16, min(4096, int(k)))
    files, walk_truncated = _walk(path, int(max_files))
    vecs, names, skipped, engines = [], [], [], {"gpu": 0, "cpu": 0}
    for fp in files:
        try:
            if os.path.getsize(fp) > int(max_file_mb) * 1024 * 1024:
                skipped.append({"file": fp, "reason": "超过单文件上限"})
                continue
            with open(fp, "rb") as f:
                data = f.read()
        except OSError as e:
            skipped.append({"file": fp, "reason": f"读取失败: {e}"})
            continue
        v, used = _sketch(data, ng, k, engine)
        engines[used] = engines.get(used, 0) + 1
        vecs.append(v)
        names.append(fp)
    n = len(vecs)
    if n < 2:
        return {"path": path, "files": n, "pairs": [], "clusters": [],
                "entropy_engine": engines, "skipped": skipped[:20],
                "walk_truncated": walk_truncated,
                "note": "少于 2 个文件，无需比较"}

    # 相似度：精确候选剪枝 → 候选上算 bottom-k Jaccard（GPU 只用于逐位置哈希）
    pairs = []
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    cand, shared_pairs = _candidate_pairs(vecs, float(threshold))
    for i, j in cand:
        s = gpu.jaccard(vecs[i], vecs[j])
        if s >= float(threshold):
            pairs.append({"a": names[i], "b": names[j], "similarity": round(s, 4)})
            union(i, j)
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(names[i])
    clusters = [sorted(v) for v in groups.values() if len(v) > 1]
    return {"path": path, "files": n, "pairs": sorted(pairs, key=lambda p: -p["similarity"]),
            "clusters": sorted(clusters), "entropy_engine": engines,
            "skipped": skipped[:20], "walk_truncated": walk_truncated,
            "candidates": len(cand), "shared_pairs": shared_pairs,
            "note": "近似聚类：GPU 两遍选择（哈希直方图定阈值 + 按阈值发射）→ "
                    "bottom-k MinHash 指纹 → 精确候选剪枝（倒排索引 + Jaccard 下界）→ "
                    "Jaccard 阈值（非逐字节 diff；阈值越高越严）。"
                    "直方图余弦对高熵数据无区分力，故不用（实测入 spec/GPU.md §二）"}
