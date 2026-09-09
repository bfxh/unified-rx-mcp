# -*- coding: utf-8 -*-
"""tools/gpu.py —— GPU 计算支持（S114）：ctypes 直调系统 OpenCL 运行时，零 pip 依赖。

设计口径（与 LSP/ast-grep 同款"可选外部引擎 + 优雅降级"）：
- **运行时**：`OpenCL.dll`（Windows）/ `libOpenCL.so`（Linux）——系统自带，不装 pip 包；
  NVIDIA 驱动即提供（本机 RTX 4060 Ti / OpenCL 3.0）。CUDA 路线需 nvcc 或 PTX，
  故不采用（nvcc 未装；OpenCL 运行时编译等价且跨厂商）。
- **诚实边界**：GPU 只加速**数据并行**内核（字面量多模式匹配、字节直方图/熵、
  异或密钥爆破）。IO 主导的活（读盘/解析/索引）不因 GPU 变快——交叉点由
  bench/s114_gpu_bench.py 实测给出，`mode="auto"` 据此选路。
- **降级纪律**：无运行时/无 GPU 设备/内核编译失败 → 明确错误或回落 CPU（不静默、
  不假装）。CPU 参考实现在同文件（`*_cpu`），既是降级路径也是 GPU 的 oracle。

内核（OpenCL C，运行时编译）：
- `literal_scan`：每 work-item 负责一个偏移，检查任一模式是否在此处出现（memcmp）。
- `byte_hist`：每 work-item 统计一个块的 256 桶直方图（原子加），用于熵/打包检测。
"""
import ctypes
import math
import os
import threading

_LOCK = threading.Lock()
_CL = None
_CTX = None
_QUEUE = None
_DEVICE = None
_PROGRAMS = {}


class GpuError(RuntimeError):
    pass


def _lib_name():
    if os.name == "nt":
        return "OpenCL.dll"
    return "libOpenCL.so"


def _cl():
    """加载并缓存 OpenCL 库 + 函数签名（argtypes 必须显式，否则 64 位句柄截断）。"""
    global _CL
    if _CL is not None:
        return _CL
    try:
        lib = ctypes.WinDLL(_lib_name()) if os.name == "nt" else ctypes.CDLL(_lib_name())
    except OSError as e:
        raise GpuError(f"OpenCL 运行时不可用（{_lib_name()} 加载失败: {e}）——"
                       f"装 GPU 驱动即可，无需 pip 包") from e
    P, U, S = ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t
    sigs = {
        "clGetPlatformIDs": ([U, ctypes.POINTER(P), ctypes.POINTER(U)], ctypes.c_int),
        "clGetPlatformInfo": ([P, U, S, ctypes.c_void_p, ctypes.POINTER(S)], ctypes.c_int),
        "clGetDeviceIDs": ([P, ctypes.c_ulonglong, U, ctypes.POINTER(P), ctypes.POINTER(U)], ctypes.c_int),
        "clGetDeviceInfo": ([P, U, S, ctypes.c_void_p, ctypes.POINTER(S)], ctypes.c_int),
        "clCreateContext": ([P, U, ctypes.POINTER(P), ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)], P),
        "clCreateCommandQueue": ([P, P, ctypes.c_ulonglong, ctypes.POINTER(ctypes.c_int)], P),
        "clCreateBuffer": ([P, ctypes.c_ulonglong, S, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)], P),
        "clCreateProgramWithSource": ([P, U, ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(S), ctypes.POINTER(ctypes.c_int)], P),
        "clBuildProgram": ([P, U, ctypes.POINTER(P), ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int),
        "clGetProgramBuildInfo": ([P, P, U, S, ctypes.c_void_p, ctypes.POINTER(S)], ctypes.c_int),
        "clCreateKernel": ([P, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)], P),
        "clSetKernelArg": ([P, U, S, ctypes.c_void_p], ctypes.c_int),
        "clEnqueueNDRangeKernel": ([P, P, U, ctypes.POINTER(S), ctypes.POINTER(S), ctypes.POINTER(S), U, ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int),
        "clEnqueueReadBuffer": ([P, P, U, S, S, ctypes.c_void_p, U, ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int),
        "clEnqueueWriteBuffer": ([P, P, U, S, S, ctypes.c_void_p, U, ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int),
        "clFinish": ([P], ctypes.c_int),
        "clReleaseMemObject": ([P], ctypes.c_int),
        "clReleaseKernel": ([P], ctypes.c_int),
        "clReleaseProgram": ([P], ctypes.c_int),
    }
    for name, (argtypes, restype) in sigs.items():
        fn = getattr(lib, name)
        fn.argtypes = argtypes
        fn.restype = restype
    _CL = lib
    return lib


def _check(err, what):
    if err != 0:
        raise GpuError(f"{what} 失败（OpenCL 错误码 {err}）")


def _dev_info(cl, dev, param, size):
    buf = ctypes.create_string_buffer(size)
    sz = ctypes.c_size_t()
    _check(cl.clGetDeviceInfo(dev, param, size, buf, ctypes.byref(sz)), "clGetDeviceInfo")
    return buf.raw[:sz.value]


def status():
    """探测结果（工具 gpu_status 的数据源）：运行时/平台/设备/降级原因。"""
    try:
        cl = _cl()
    except GpuError as e:
        return {"available": False, "reason": str(e)}
    n = ctypes.c_uint()
    if cl.clGetPlatformIDs(0, None, ctypes.byref(n)) != 0 or n.value == 0:
        return {"available": False, "reason": "无 OpenCL 平台（装 GPU 驱动）"}
    plats = (ctypes.c_void_p * n.value)()
    cl.clGetPlatformIDs(n.value, plats, None)
    out = {"available": False, "runtime": _lib_name(), "platforms": []}
    for p in plats:
        def pinfo(param, size=256):
            buf = ctypes.create_string_buffer(size)
            sz = ctypes.c_size_t()
            _check(cl.clGetPlatformInfo(p, param, size, buf, ctypes.byref(sz)), "clGetPlatformInfo")
            return buf.value.decode("utf-8", "replace")
        pobj = {"name": pinfo(0x0902), "vendor": pinfo(0x0903),
                "version": pinfo(0x0901), "devices": []}
        dn = ctypes.c_uint()
        if cl.clGetDeviceIDs(p, 4, 0, None, ctypes.byref(dn)) == 0 and dn.value:  # GPU
            devs = (ctypes.c_void_p * dn.value)()
            cl.clGetDeviceIDs(p, 4, dn.value, devs, None)
            for d in devs:
                cu = ctypes.c_uint()
                gm = ctypes.c_ulonglong()
                cl.clGetDeviceInfo(d, 0x1002, 4, ctypes.byref(cu), None)
                cl.clGetDeviceInfo(d, 0x101F, 8, ctypes.byref(gm), None)
                pobj["devices"].append({
                    "name": _dev_info(cl, d, 0x102B, 256).decode("utf-8", "replace").strip("\x00"),
                    "compute_units": cu.value,
                    "vram_gb": round(gm.value / (1024 ** 3), 2)})
        out["platforms"].append(pobj)
        if pobj["devices"]:
            out["available"] = True
    if not out["available"]:
        out["reason"] = "有 OpenCL 平台但无 GPU 设备"
    return out


def _ensure_ctx():
    """首次使用时建上下文/队列（GPU 优先；失败抛 GpuError）。"""
    global _CTX, _QUEUE, _DEVICE
    if _CTX is not None:
        return _CTX, _QUEUE, _DEVICE
    with _LOCK:
        if _CTX is not None:
            return _CTX, _QUEUE, _DEVICE
        cl = _cl()
        n = ctypes.c_uint()
        _check(cl.clGetPlatformIDs(0, None, ctypes.byref(n)), "clGetPlatformIDs")
        if n.value == 0:
            raise GpuError("无 OpenCL 平台")
        plats = (ctypes.c_void_p * n.value)()
        cl.clGetPlatformIDs(n.value, plats, None)
        for p in plats:
            dn = ctypes.c_uint()
            if cl.clGetDeviceIDs(p, 4, 0, None, ctypes.byref(dn)) != 0 or dn.value == 0:
                continue
            devs = (ctypes.c_void_p * dn.value)()
            cl.clGetDeviceIDs(p, 4, dn.value, devs, None)
            err = ctypes.c_int()
            one = (ctypes.c_void_p * 1)(devs[0])
            ctx = cl.clCreateContext(None, 1, one, None, None, ctypes.byref(err))
            if not ctx:
                continue
            queue = cl.clCreateCommandQueue(ctx, devs[0], 0, ctypes.byref(err))
            if not queue:
                continue
            _CTX, _QUEUE, _DEVICE = ctx, queue, devs[0]
            return _CTX, _QUEUE, _DEVICE
        raise GpuError("无可用 GPU 设备（OpenCL GPU 类型）")


def _set_arg(cl, kern, idx, cvalue):
    """统一 clSetKernelArg：cvalue 必须是 ctypes 对象（c_void_p/c_uint/…）。"""
    _check(cl.clSetKernelArg(kern, idx, ctypes.sizeof(cvalue), ctypes.byref(cvalue)),
           f"clSetKernelArg({idx})")


def _program(src):
    """编译并缓存 OpenCL 程序（同一源码只编译一次）。"""
    key = hash(src)
    if key in _PROGRAMS:
        return _PROGRAMS[key]
    cl = _cl()
    ctx, _, dev = _ensure_ctx()
    err = ctypes.c_int()
    sbuf = src.encode("utf-8")
    prog = cl.clCreateProgramWithSource(ctx, 1, ctypes.byref(ctypes.c_char_p(sbuf)),
                                        None, ctypes.byref(err))
    _check(err.value, "clCreateProgramWithSource")
    one_dev = (ctypes.c_void_p * 1)(dev)
    rc = cl.clBuildProgram(prog, 1, one_dev, b"-cl-fast-relaxed-math",
                           None, None)
    if rc != 0:
        sz = ctypes.c_size_t()
        cl.clGetProgramBuildInfo(prog, dev, 0x1183, 0, None, ctypes.byref(sz))  # CL_PROGRAM_BUILD_LOG
        log = ctypes.create_string_buffer(sz.value)
        cl.clGetProgramBuildInfo(prog, dev, 0x1183, sz.value, log, None)
        raise GpuError("内核编译失败: " + log.value.decode("utf-8", "replace")[:400])
    _PROGRAMS[key] = prog
    return prog


_K_LITERAL = r"""
__kernel void literal_scan(__global const uchar* data, const uint n,
                           __global const uchar* pats, __global const uint* poff,
                           const uint plen_total, const uint npats,
                           __global uint* hits) {
    uint i = get_global_id(0);
    if (i >= n) return;
    for (uint k = 0; k < npats; ++k) {
        uint off = poff[k], plen = poff[k+1] - off;
        if (plen == 0 || i + plen > n) continue;
        uint ok = 1;
        for (uint j = 0; j < plen; ++j) {
            if (data[i + j] != pats[off + j]) { ok = 0; break; }
        }
        if (ok) { hits[i] = k + 1; return; }
    }
}
"""

_K_HIST = r"""
__kernel void byte_hist(__global const uchar* data, const uint n,
                        __global uint* hist) {
    uint gid = get_global_id(0), gsz = get_global_size(0);
    uint bins[256];
    for (uint i = 0; i < 256; ++i) bins[i] = 0;
    for (uint i = gid; i < n; i += gsz) bins[data[i]]++;
    for (uint i = 0; i < 256; ++i) {
        if (bins[i]) atomic_add(&hist[i], bins[i]);
    }
}
"""


_K_XOR = r"""
__kernel void xor_crib_scan(__global const uchar* data, const uint n,
                            __global const uchar* crib, const uint clen,
                            __global const uchar* keys, const uint nkeys,
                            __global uint* hits) {
    uint k = get_global_id(0);
    if (k >= nkeys) return;
    uchar key = keys[k];
    uint count = 0;
    for (uint i = 0; i + clen <= n; ++i) {
        uint ok = 1;
        for (uint j = 0; j < clen; ++j) {
            if ((data[i + j] ^ key) != crib[j]) { ok = 0; break; }
        }
        if (ok && count < 256) count++;
    }
    hits[k] = count;
}
"""

def _read_buf(cl, queue, mem, nbytes):
    out = (ctypes.c_char * nbytes)()
    _check(cl.clEnqueueReadBuffer(queue, mem, 1, 0, nbytes, out, 0, None, None),
           "clEnqueueReadBuffer")
    cl.clFinish(queue)
    return bytes(out)


def literal_scan_gpu(data, patterns):
    """GPU 多模式字面量匹配 → [(offset, pattern_index)]（同偏移多模式只报第一个）。"""
    cl = _cl()
    ctx, queue, _ = _ensure_ctx()
    pats = [p for p in patterns if p]
    if not data or not pats:
        return []
    flat = b"".join(pats)
    poff = [0]
    for p in pats:
        poff.append(poff[-1] + len(p))
    err = ctypes.c_int()
    n = len(data)
    dmem = cl.clCreateBuffer(ctx, 4 | 32, n, ctypes.c_char_p(data), ctypes.byref(err))
    _check(err.value, "clCreateBuffer(data)")
    pmem = cl.clCreateBuffer(ctx, 4 | 32, len(flat), ctypes.c_char_p(flat), ctypes.byref(err))
    _check(err.value, "clCreateBuffer(pats)")
    omem = cl.clCreateBuffer(ctx, 4 | 32, 4 * (len(poff)), (ctypes.c_uint * len(poff))(*poff),
                             ctypes.byref(err))
    _check(err.value, "clCreateBuffer(poff)")
    hmem = cl.clCreateBuffer(ctx, 2, 4 * n, None, ctypes.byref(err))
    _check(err.value, "clCreateBuffer(hits)")
    zero = (ctypes.c_char * (4 * n))()
    _check(cl.clEnqueueWriteBuffer(queue, hmem, 1, 0, 4 * n, zero, 0, None, None), "写零 hits")
    kern = cl.clCreateKernel(_program(_K_LITERAL), b"literal_scan", ctypes.byref(err))
    _check(err.value, "clCreateKernel")
    _set_arg(cl, kern, 0, ctypes.c_void_p(dmem))
    _set_arg(cl, kern, 1, ctypes.c_uint(n))
    _set_arg(cl, kern, 2, ctypes.c_void_p(pmem))
    _set_arg(cl, kern, 3, ctypes.c_void_p(omem))
    _set_arg(cl, kern, 4, ctypes.c_uint(len(flat)))
    _set_arg(cl, kern, 5, ctypes.c_uint(len(pats)))
    _set_arg(cl, kern, 6, ctypes.c_void_p(hmem))
    gsz = ctypes.c_size_t(n)
    _check(cl.clEnqueueNDRangeKernel(queue, kern, 1, None, ctypes.byref(gsz), None, 0, None, None),
           "clEnqueueNDRangeKernel")
    raw = _read_buf(cl, queue, hmem, 4 * n)
    hits = []
    for i in range(n):
        v = int.from_bytes(raw[4 * i:4 * i + 4], "little")
        if v:
            hits.append((i, v - 1))
    for h in (dmem, pmem, omem, hmem):
        cl.clReleaseMemObject(h)
    cl.clReleaseKernel(kern)
    return hits


def literal_scan_cpu(data, patterns):
    """CPU 参考实现（降级路径 + GPU 的 oracle）：bytes.find 逐个模式取所有出现位置。"""
    hits = []
    for i, p in enumerate(patterns):
        if not p:
            continue
        start = 0
        while True:
            j = data.find(p, start)
            if j < 0:
                break
            hits.append((j, i))
            start = j + 1
    hits.sort()
    return hits


def byte_hist_gpu(data):
    """GPU 字节直方图（256 桶）→ list[int]。"""
    cl = _cl()
    ctx, queue, _ = _ensure_ctx()
    n = len(data)
    if not n:
        return [0] * 256
    err = ctypes.c_int()
    dmem = cl.clCreateBuffer(ctx, 4 | 32, n, ctypes.c_char_p(data), ctypes.byref(err))
    _check(err.value, "clCreateBuffer(data)")
    hmem = cl.clCreateBuffer(ctx, 2 | 32, 4 * 256, (ctypes.c_uint * 256)(*([0] * 256)),
                             ctypes.byref(err))
    _check(err.value, "clCreateBuffer(hist)")
    kern = cl.clCreateKernel(_program(_K_HIST), b"byte_hist", ctypes.byref(err))
    _check(err.value, "clCreateKernel")
    _set_arg(cl, kern, 0, ctypes.c_void_p(dmem))
    _set_arg(cl, kern, 1, ctypes.c_uint(n))
    _set_arg(cl, kern, 2, ctypes.c_void_p(hmem))
    gsz = ctypes.c_size_t(min(65536, max(1, n // 64)))
    _check(cl.clEnqueueNDRangeKernel(queue, kern, 1, None, ctypes.byref(gsz), None, 0, None, None),
           "clEnqueueNDRangeKernel")
    raw = _read_buf(cl, queue, hmem, 4 * 256)
    hist = [int.from_bytes(raw[4 * i:4 * i + 4], "little") for i in range(256)]
    cl.clReleaseMemObject(dmem)
    cl.clReleaseMemObject(hmem)
    cl.clReleaseKernel(kern)
    return hist


def byte_hist_cpu(data):
    hist = [0] * 256
    for b in data:
        hist[b] += 1
    return hist


def entropy(hist, n):
    """香农熵（0-8 bits/byte）——直方图直接算。"""
    if not n:
        return 0.0
    h = 0.0
    for c in hist:
        if c:
            p = c / n
            h -= p * math.log2(p)
    return h


# ---- 交叉点（bench/s114_gpu_bench.py 实测回填，2026-09-09 / RTX 4060 Ti）----
# 实测：byte_hist GPU 31-38×（1-64MB，预热后，结果与 CPU 逐位一致）；
#       literal_scan GPU 慢 5-6×（CPU bytes.find/memmem 太强）→ auto 恒走 CPU。
# 换硬件/换数据分布请重跑 bench/s114_gpu_bench.py 再回填。
CROSSOVER = {"literal_scan_bytes": 1 << 62, "byte_hist_bytes": 1 << 20,
             # 实测：1MB 0.6× / 4MB 3.8× → 取 2MB 为界
             "xor_scan_bytes": 2 << 20}
# S119 退役：dot_matrix（无调用方 + 朴素内核实测 22× 慢于原生 Rust）——见 spec/GPU.md §二


def pick_mode(kind, nbytes, mode="auto"):
    if mode in ("cpu", "gpu"):
        return mode
    return "gpu" if nbytes >= CROSSOVER.get(kind, 1 << 62) else "cpu"


# ---------- 工具：gpu_status（遥测；meta 域） ----------
from registry import tool  # noqa: E402


@tool("gpu_status", "GPU 计算支持状态：OpenCL 运行时/设备（名称/CU/VRAM）与降级原因"
      "——量大的统计类活走 GPU（实测熵计算 31-38×），IO/编排类不接", "meta",
      {"type": "object", "properties": {}, "required": []})
def gpu_status():
    st = status()
    st["note"] = ("GPU 只加速数据并行内核（直方图/熵等）；字面量匹配与 IO/编排类"
                  "仍走 CPU（交叉点实测见 spec/GPU.md）；auto 按实测选路")
    return st

def xor_crib_scan_gpu(data, crib, keys=range(256)):
    """单字节异或密钥枚举：找让 crib 在解码后出现的密钥 → [(key, count)]。

    典型用途：恶意样本/混淆载荷的单字节 XOR 层探测（配已知明文 crib，如 "MZ"、
    "PK"、EICAR 串）。多字节 XOR 需先爆破密钥空间，本函数不做（如实边界）。
    """
    cl = _cl()
    ctx, queue, _ = _ensure_ctx()
    keys = list(keys)
    if not data or not crib or not keys:
        return []
    err = ctypes.c_int()
    n = len(data)
    dmem = cl.clCreateBuffer(ctx, 4 | 32, n, ctypes.c_char_p(data), ctypes.byref(err))
    _check(err.value, "clCreateBuffer(data)")
    cmem = cl.clCreateBuffer(ctx, 4 | 32, len(crib), ctypes.c_char_p(crib), ctypes.byref(err))
    _check(err.value, "clCreateBuffer(crib)")
    kmem = cl.clCreateBuffer(ctx, 4 | 32, len(keys), (ctypes.c_ubyte * len(keys))(*keys),
                             ctypes.byref(err))
    _check(err.value, "clCreateBuffer(keys)")
    hmem = cl.clCreateBuffer(ctx, 2, 4 * len(keys), None, ctypes.byref(err))
    _check(err.value, "clCreateBuffer(hits)")
    kern = cl.clCreateKernel(_program(_K_XOR), b"xor_crib_scan", ctypes.byref(err))
    _check(err.value, "clCreateKernel")
    _set_arg(cl, kern, 0, ctypes.c_void_p(dmem))
    _set_arg(cl, kern, 1, ctypes.c_uint(n))
    _set_arg(cl, kern, 2, ctypes.c_void_p(cmem))
    _set_arg(cl, kern, 3, ctypes.c_uint(len(crib)))
    _set_arg(cl, kern, 4, ctypes.c_void_p(kmem))
    _set_arg(cl, kern, 5, ctypes.c_uint(len(keys)))
    _set_arg(cl, kern, 6, ctypes.c_void_p(hmem))
    gsz = ctypes.c_size_t(len(keys))
    _check(cl.clEnqueueNDRangeKernel(queue, kern, 1, None, ctypes.byref(gsz), None, 0, None, None),
           "clEnqueueNDRangeKernel")
    raw = _read_buf(cl, queue, hmem, 4 * len(keys))
    out = []
    for i, k in enumerate(keys):
        c = int.from_bytes(raw[4 * i:4 * i + 4], "little")
        if c:
            out.append((k, c))
    for m in (dmem, cmem, kmem, hmem):
        cl.clReleaseMemObject(m)
    cl.clReleaseKernel(kern)
    return out


def xor_crib_scan_cpu(data, crib, keys=range(256)):
    """CPU 参考（降级 + oracle）：bytes.translate 建 XOR 表 + find（C 速度）。"""
    out = []
    for k in keys:
        table = bytes((i ^ k) for i in range(256))
        decoded = data.translate(table)
        start, c = 0, 0
        while True:
            j = decoded.find(crib, start)
            if j < 0:
                break
            if c < 256:
                c += 1
            start = j + 1
        if c:
            out.append((k, c))
    return out


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
# 开销 ~4ms）→ 交叉点取 8KB。全量哈希输出仅 2.8×（输出带宽受限），故 sketch 走两遍
# 选择，该口径只留作回退路径与对拍 oracle。**负结果入册**：批量哈希（FNV-1a 64 每块）
# GPU vs CPU hashlib.blake2b 仅 0.4-1.2× —— CPU 的 C 实现更快，故**不接**（删内核避免
# 死代码；数字见 spec/GPU.md §二）。
CROSSOVER.update({"ngram_bottomk_bytes": 1 << 13, "ngram_hashes_bytes": 2 << 20})

