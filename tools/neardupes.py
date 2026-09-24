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
import ctypes
import json
import math
import os
import struct
import subprocess

from registry import tool
from tools import gpu
from tools.filewalk import TOOLCHAIN_SKIP_DIRS, iter_files
from tools.fs import _resolve as _fs_resolve

# S130：GPU kernel 就近迁移到本域（见文件尾）——运行时助手自 gpu 引入
from tools.gpu import _check, _cl, _ensure_ctx, _program, _read_buf, _set_arg

_RX_SCAN_EXE_NAME = "rx-scan.exe"


def _walk(root, max_files):
    """遍历 → (文件列表, 是否因上限截断)。S129/C1b：收敛到 tools/filewalk 单一实现。

    截断必须如实上报：静默丢尾会让"扫描结果"看起来是全量（S118 修）——
    多取 1 个判截断，语义与旧实现逐字等价（恰好 max_files 个时不标截断）。
    """
    files = list(iter_files(root, max_files + 1, lambda _fp: True,
                            TOOLCHAIN_SKIP_DIRS, sort_files=True))
    truncated = len(files) > max_files
    return files[:max_files], truncated


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
            return ngram_bottomk_gpu(data, ng, k), "gpu"
        except gpu.GpuError:
            pass
    return ngram_bottomk_cpu(data, ng, k), "cpu"


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


@tool("near_dupes", "近似重复/同族聚类：MinHash 指纹 + Jaccard（rust/GPU/CPU 三档如实上报，精确候选剪枝不丢真对）——重复代码分堆、样本归并", "scan",
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
        s = jaccard(vecs[i], vecs[j])
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


# ---------- GPU kernel 簇（S130 自 tools/gpu.py 逐字迁移，CONSOLIDATION §三 P1 gpu）----------
# 运行时（OpenCL 加载/上下文/编译缓存/参数/读回）在 tools/gpu.py；本簇含 kernel 源码、
# GPU 实现与 CPU oracle（降级路径 + 对拍基准）。公开名不变（域内直呼）。

_K_NGBUCKET = r"""
__kernel void ngram_buckets(__global const uchar* data, const uint n, const uint ng,
                            const uint shift, __global uint* bins) {
    uint gid = get_global_id(0), gsz = get_global_size(0);
    for (uint i = gid; i + ng <= n; i += gsz) {
        uint h = 2166136261u;                 /* FNV-1a 32 */
        for (uint j = 0; j < ng; ++j) h = (h ^ (uint)data[i + j]) * 16777619u;
        atomic_add(&bins[h >> shift], 1u);
    }
}
"""

_K_NGEMIT = r"""
__kernel void ngram_emit(__global const uchar* data, const uint n, const uint ng,
                         const uint shift, const uint hi, const uint cap,
                         __global uint* out, __global uint* count) {
    uint gid = get_global_id(0), gsz = get_global_size(0);
    for (uint i = gid; i + ng <= n; i += gsz) {
        uint h = 2166136261u;
        for (uint j = 0; j < ng; ++j) h = (h ^ (uint)data[i + j]) * 16777619u;
        if ((h >> shift) <= hi) {
            uint idx = atomic_inc(&count[0]);
            if (idx < cap) out[idx] = h;
        }
    }
}
"""

_NGBUCKET_SHIFT = 20          # 4096 桶：按哈希高 12 位定阈值，桶边界即阈值边界


def ngram_bottomk_gpu(data, ng, k=128):
    """bottom-k MinHash 指纹（GPU 两遍选择；FNV-1a 32 口径，与 CPU 参考逐位一致）。

    第一遍按哈希高 12 位做直方图，取累计 ≥ 4k 的最小桶边界为阈值；
    第二遍只发射 ≤ 阈值的哈希（期望 ~4k 个）。回传量 O(n)→O(k)，
    大文件不再受输出带宽限制（全量输出实测仅 2.8×，见 spec/GPU.md §二）。
    单桶超过容量上限的极端分布自动回退全量哈希路径，结果口径不变。
    """
    cl = _cl()
    ctx, queue, _ = _ensure_ctx()
    n = len(data)
    m = n - ng + 1
    if m <= 0 or ng < 1:
        return frozenset()
    err = ctypes.c_int()
    dmem = cl.clCreateBuffer(ctx, 4 | 32, n, ctypes.c_char_p(data), ctypes.byref(err))
    _check(err.value, "clCreateBuffer(data)")
    shift = _NGBUCKET_SHIFT
    nb = 1 << (32 - shift)
    try:
        bmem = cl.clCreateBuffer(ctx, 2 | 32, 4 * nb,
                                 (ctypes.c_uint * nb)(*([0] * nb)), ctypes.byref(err))
        _check(err.value, "clCreateBuffer(bins)")
        kern = cl.clCreateKernel(_program(_K_NGBUCKET), b"ngram_buckets", ctypes.byref(err))
        _check(err.value, "clCreateKernel(buckets)")
        _set_arg(cl, kern, 0, ctypes.c_void_p(dmem))
        _set_arg(cl, kern, 1, ctypes.c_uint(n))
        _set_arg(cl, kern, 2, ctypes.c_uint(ng))
        _set_arg(cl, kern, 3, ctypes.c_uint(shift))
        _set_arg(cl, kern, 4, ctypes.c_void_p(bmem))
        gsz = ctypes.c_size_t(min(65536, max(1, m // 16)))
        _check(cl.clEnqueueNDRangeKernel(queue, kern, 1, None, ctypes.byref(gsz),
                                         None, 0, None, None),
               "clEnqueueNDRangeKernel(buckets)")
        raw = _read_buf(cl, queue, bmem, 4 * nb)
        cl.clReleaseMemObject(bmem)
        cl.clReleaseKernel(kern)
        hist = [int.from_bytes(raw[4 * i:4 * i + 4], "little") for i in range(nb)]
        target = max(4 * int(k), 64)
        cum, hi = 0, nb - 1
        for b, c in enumerate(hist):
            cum += c
            if cum >= target:
                hi = b
                break
        cap = min(m, max(1 << 16, 16 * int(k)))
        omem = cl.clCreateBuffer(ctx, 2, 4 * cap, None, ctypes.byref(err))
        _check(err.value, "clCreateBuffer(out)")
        cmem = cl.clCreateBuffer(ctx, 2 | 32, 4, (ctypes.c_uint * 1)(0), ctypes.byref(err))
        _check(err.value, "clCreateBuffer(count)")
        kern2 = cl.clCreateKernel(_program(_K_NGEMIT), b"ngram_emit", ctypes.byref(err))
        _check(err.value, "clCreateKernel(emit)")
        _set_arg(cl, kern2, 0, ctypes.c_void_p(dmem))
        _set_arg(cl, kern2, 1, ctypes.c_uint(n))
        _set_arg(cl, kern2, 2, ctypes.c_uint(ng))
        _set_arg(cl, kern2, 3, ctypes.c_uint(shift))
        _set_arg(cl, kern2, 4, ctypes.c_uint(hi))
        _set_arg(cl, kern2, 5, ctypes.c_uint(cap))
        _set_arg(cl, kern2, 6, ctypes.c_void_p(omem))
        _set_arg(cl, kern2, 7, ctypes.c_void_p(cmem))
        gsz = ctypes.c_size_t(m)
        _check(cl.clEnqueueNDRangeKernel(queue, kern2, 1, None, ctypes.byref(gsz),
                                         None, 0, None, None),
               "clEnqueueNDRangeKernel(emit)")
        cnt = int.from_bytes(_read_buf(cl, queue, cmem, 4), "little")
        cl.clReleaseMemObject(cmem)
        cl.clReleaseKernel(kern2)
        if cnt > cap:
            cl.clReleaseMemObject(omem)
            return bottom_k(ngram_hashes_gpu(data, ng), k)
        raw = _read_buf(cl, queue, omem, 4 * cnt) if cnt else b""
        cl.clReleaseMemObject(omem)
        got = [int.from_bytes(raw[4 * i:4 * i + 4], "little") for i in range(cnt)]
        return bottom_k(got, k)
    finally:
        cl.clReleaseMemObject(dmem)


_K_NGHASH = r"""
__kernel void ngram_hashes(__global const uchar* data, const uint n, const uint ng,
                           __global uint* out) {
    uint i = get_global_id(0);
    if (i + ng > n) return;
    uint h = 2166136261u;
    for (uint j = 0; j < ng; ++j) h = (h ^ (uint)data[i + j]) * 16777619u;
    out[i] = h;
}
"""


def ngram_hashes_gpu(data, ng):
    """每位置一个 n-gram 哈希（FNV-1a 32）→ list[int]（len = n-ng+1）。"""
    cl = _cl()
    ctx, queue, _ = _ensure_ctx()
    n = len(data)
    m = n - ng + 1
    if m <= 0:
        return []
    err = ctypes.c_int()
    dmem = cl.clCreateBuffer(ctx, 4 | 32, n, ctypes.c_char_p(data), ctypes.byref(err))
    _check(err.value, "clCreateBuffer(data)")
    omem = cl.clCreateBuffer(ctx, 2, 4 * m, None, ctypes.byref(err))
    _check(err.value, "clCreateBuffer(out)")
    kern = cl.clCreateKernel(_program(_K_NGHASH), b"ngram_hashes", ctypes.byref(err))
    _check(err.value, "clCreateKernel")
    _set_arg(cl, kern, 0, ctypes.c_void_p(dmem))
    _set_arg(cl, kern, 1, ctypes.c_uint(n))
    _set_arg(cl, kern, 2, ctypes.c_uint(ng))
    _set_arg(cl, kern, 3, ctypes.c_void_p(omem))
    gsz = ctypes.c_size_t(m)
    _check(cl.clEnqueueNDRangeKernel(queue, kern, 1, None, ctypes.byref(gsz), None, 0, None, None),
           "clEnqueueNDRangeKernel")
    raw = _read_buf(cl, queue, omem, 4 * m)
    out = [int.from_bytes(raw[4 * i:4 * i + 4], "little") for i in range(m)]
    cl.clReleaseMemObject(dmem)
    cl.clReleaseMemObject(omem)
    cl.clReleaseKernel(kern)
    return out


def ngram_hashes_cpu(data, ng):
    """CPU 参考（同 FNV-1a 32 口径；纯 Python 基线）。"""
    out = []
    n = len(data)
    for i in range(max(0, n - ng + 1)):
        h = 2166136261
        for j in range(ng):
            h = ((h ^ data[i + j]) * 16777619) & 0xFFFFFFFF
        out.append(h)
    return out


def bottom_k(hashes, k=128):
    """bottom-k MinHash 指纹：取 k 个最小且互异的哈希（近重复检测经典口径）。"""
    import heapq
    if not hashes:
        return frozenset()
    return frozenset(heapq.nsmallest(int(k), hashes))


def jaccard(a, b):
    if not a and not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def ngram_bottomk_cpu(data, ng, k=128):
    """CPU 参考：独立算法（全量哈希 + heapq 选择），用作 GPU 路径的 oracle 对拍。"""
    return bottom_k(ngram_hashes_cpu(data, ng), k)


# 实测（2026-09-09，RTX 4060 Ti）：bottom-k 两遍选择 GPU vs CPU 参考——2.1×@8KB、
# 12.3×@64KB、49.7×@256KB、187×@1MB、281×@4MB、551×@16MB（4KB 时 0.9×，GPU 固定
# 开销 ~4ms）→ 交叉点取 8KB（tools/gpu.py CROSSOVER）。全量哈希输出仅 2.8×（输出带宽
# 受限），故 sketch 走两遍选择，该口径只留作回退路径与对拍 oracle。**负结果入册**：
# 批量哈希（FNV-1a 64 每块）GPU vs CPU hashlib.blake2b 仅 0.4-1.2× —— CPU 的 C 实现
# 更快，故**不接**（删内核避免死代码；数字见 spec/GPU.md §二）。
