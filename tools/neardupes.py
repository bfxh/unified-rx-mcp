# -*- coding: utf-8 -*-
"""tools/neardupes.py —— 近似重复/同族文件聚类（S117）：bottom-k MinHash 指纹 + Jaccard。

用途：重复代码分堆、样本同族归并、大目录里找"几乎一样"的文件。

指纹引擎三档（S119 起 Rust 优先，实测见 spec/GPU.md §二·三）：
- **rust**：`rx-scan sketch` 一次进程调用批量算（std::thread 分块并行）——
  16MB 5.4ms、300×20KB 约 15ms，比 GPU 逐文件快 6-13×；
- **gpu**：两遍选择（直方图定阈值 + 按阈值发射，回传量 O(n)→O(k)）；
- **cpu**：纯 Python 参考实现（无 exe/无 GPU 时的兜底，也是 oracle）。
IO（读盘/遍历）始终在 Python 侧（单点所有权：遍历过滤只此一份）。

S118 规模化：两两比较从 O(n²) 全对降为**精确候选剪枝**（倒排索引 + Jaccard 下界，
不丢真对），并把"文件被上限截断"如实标进 `walk_truncated`（此前静默丢尾）。

口径：**近似**——bottom-k MinHash + Jaccard 阈值，不是逐字节 diff；阈值越高越严。
直方图余弦对高熵数据无区分力（随机文件也 0.98），故不用（实测入 spec/GPU.md §二）。
"""
import json
import math
import os
import struct
import subprocess

from registry import tool
from tools import gpu
from tools.fs import _resolve as _fs_resolve

_SKIP_DIRS = {".git", "node_modules", "target", "__pycache__", "dist", "build",
              ".venv", "venv", ".pytest_cache"}

_RX_SCAN_EXE_NAME = "rx-scan.exe"


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


def _rx_scan_exe():
    """定位 rx-scan.exe：UNIFIED_RX_RS_EXE 覆盖 → cargo 目标目录惯例路径。

    与 tools/scan.py::_rx_scan_exe 同纪律：候选必须已存在且文件名恰为
    rx-scan.exe（argv 固定前缀、list 形式、无 shell，env 覆盖不构成任意
    命令执行面）。
    """
    cand = []
    override = os.environ.get("UNIFIED_RX_RS_EXE")
    if override:
        cand.append(override)
    tmp = os.environ.get("TEMP", r"C:\Temp")
    cand += [os.path.join(tmp, "rx-rs-target", kind, _RX_SCAN_EXE_NAME)
             for kind in ("release", "debug")]
    for c in cand:
        if os.path.isfile(c) and os.path.basename(c) == _RX_SCAN_EXE_NAME:
            return c
    return None


def _rust_sketch(files, ng, k):
    """批量指纹走 rx-scan.exe sketch（一次调用，路径经 stdin 帧流）。

    返回 ({path: frozenset}, {path: 读取错误})；exe 缺失/超时/非 JSON → ValueError
    （由调用方回落并**如实上报** fallback 原因，不静默降级）。
    """
    exe = _rx_scan_exe()
    if not exe:
        raise ValueError("rx-scan.exe 不存在——先在 rust/ 下 cargo build --release "
                         "（或设 UNIFIED_RX_RS_EXE 指向现有 exe）")
    buf = b"".join(struct.pack("<I", len(p.encode("utf-8"))) + p.encode("utf-8")
                   for p in files) + struct.pack("<I", 0)
    try:
        cp = subprocess.run([exe, "sketch", str(ng), str(k)], capture_output=True,
                            timeout=120, input=buf)
    except subprocess.TimeoutExpired:
        raise ValueError("rx-scan sketch 超时（120s）")
    tail = (cp.stderr or b"").decode("utf-8", errors="replace").strip()[-300:]
    lines = (cp.stdout or b"").decode("utf-8", errors="replace").strip().splitlines()
    if not lines:
        raise ValueError(f"rx-scan sketch 无输出（exit={cp.returncode}）: {tail}")
    try:
        out = json.loads(lines[-1])
    except ValueError:
        raise ValueError(f"rx-scan sketch 输出非 JSON: {lines[-1][:200]}")
    if cp.returncode != 0 or not isinstance(out, dict) or "files" not in out:
        raise ValueError(f"rx-scan sketch 失败（exit={cp.returncode}）: {tail or lines[-1][:200]}")
    table = {ent["path"]: frozenset(int(h) for h in ent.get("fingerprint", []))
             for ent in out["files"]}
    errors = {ent["path"]: ent.get("error", "读取失败") for ent in out.get("errors", [])}
    return table, errors


def _sketch_one(data, ng, k, engine):
    """单文件指纹：GPU 两遍选择（按实测交叉点）→ CPU 参考实现兜底。"""
    mode = gpu.pick_mode("ngram_bottomk_bytes", len(data), engine)
    if mode == "gpu":
        try:
            return gpu.ngram_bottomk_gpu(data, ng, k), "gpu"
        except gpu.GpuError:
            pass
    return gpu.ngram_bottomk_cpu(data, ng, k), "cpu"


def _fingerprints(files, ng, k, engine, skipped):
    """按引擎取指纹 → (vecs, names, engines, fallback)。

    rust（auto 下 exe 在就用）→ 一次进程调用；gpu/cpu → 逐文件。
    强制 rust 但 exe 缺失/失败 → 回落逐文件并上报 fallback 原因。
    """
    engines = {"rust": 0, "gpu": 0, "cpu": 0}
    vecs, names, notes = [], [], []
    use_rust = engine == "rust" or (engine == "auto" and _rx_scan_exe() is not None)
    per_file = list(files)
    if use_rust:
        enc_ok = []
        per_file = []
        for fp in files:
            try:
                fp.encode("utf-8")
            except UnicodeEncodeError:
                per_file.append(fp)
            else:
                enc_ok.append(fp)
        if per_file:
            notes.append(f"{len(per_file)} 个路径含不可编码字符，转逐文件通道")
        try:
            table, errors = _rust_sketch(enc_ok, ng, k)
        except ValueError as e:
            notes.append(str(e))
            per_file = list(files)
        else:
            for fp in enc_ok:
                if fp in errors:
                    skipped.append({"file": fp, "reason": f"Rust 读取失败: {errors[fp]}"})
                    continue
                vecs.append(table.get(fp, frozenset()))
                names.append(fp)
                engines["rust"] += 1
    for fp in per_file:
        try:
            with open(fp, "rb") as f:
                data = f.read()
        except OSError as e:
            skipped.append({"file": fp, "reason": f"读取失败: {e}"})
            continue
        v, used = _sketch_one(data, ng, k, "auto" if engine == "rust" else engine)
        engines[used] += 1
        vecs.append(v)
        names.append(fp)
    return vecs, names, engines, ("；".join(notes) or None)


@tool("near_dupes", "近似重复/同族文件聚类：bottom-k MinHash 指纹 + Jaccard 聚类"
      "（Rust 批量 5.4ms@16MB / GPU 两遍选择 / CPU 参考，三档如实上报；"
      "两两比较走精确候选剪枝，不丢真对）——重复代码分堆、样本同族归并", "scan",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "目录（沙盒内）"},
           "ng": {"type": "integer", "description": "n-gram 长度（默认 4）"},
           "k": {"type": "integer", "description": "bottom-k 指纹长度（默认 128）"},
           "threshold": {"type": "number", "description": "Jaccard 阈值（默认 0.8）"},
           "max_files": {"type": "integer", "description": "文件上限（默认 100）"},
           "max_file_mb": {"type": "integer", "description": "单文件上限 MB（默认 64）"},
           "engine": {"type": "string", "enum": ["auto", "rust", "gpu", "cpu"],
                      "description": "引擎（默认 auto：exe 在走 rust，否则按交叉点）"},
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
    skipped = []
    eligible = []
    cap = int(max_file_mb) * 1024 * 1024
    for fp in files:
        try:
            if os.path.getsize(fp) > cap:
                skipped.append({"file": fp, "reason": "超过单文件上限"})
                continue
        except OSError as e:
            skipped.append({"file": fp, "reason": f"读取失败: {e}"})
            continue
        eligible.append(fp)
    vecs, names, engines, sketch_fallback = _fingerprints(
        eligible, ng, k, engine, skipped)
    n = len(vecs)
    if n < 2:
        return {"path": path, "files": n, "pairs": [], "clusters": [],
                "sketch_engine": engines, "sketch_fallback": sketch_fallback,
                "skipped": skipped[:20], "walk_truncated": walk_truncated,
                "note": "少于 2 个文件，无需比较"}

    # 相似度：精确候选剪枝 → 候选上算 bottom-k Jaccard（指纹阶段已定引擎）
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
            "clusters": sorted(clusters), "sketch_engine": engines,
            "sketch_fallback": sketch_fallback,
            "skipped": skipped[:20], "walk_truncated": walk_truncated,
            "candidates": len(cand), "shared_pairs": shared_pairs,
            "note": "近似聚类：指纹（rust 批量 / GPU 两遍选择 / CPU 参考，见 sketch_engine）"
                    "→ 精确候选剪枝（倒排索引 + Jaccard 下界）→ Jaccard 阈值"
                    "（非逐字节 diff；阈值越高越严）。直方图余弦对高熵数据无区分力，"
                    "故不用（实测入 spec/GPU.md §二）"}
