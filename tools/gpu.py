"""tools/gpu.py —— GPU 计算运行时与选路（S114；S130 拆分：kernel 就近迁移）。

S130 拆分（CONSOLIDATION §三 P1 gpu）：本文件收敛为**运行时**——OpenCL 加载/
上下文/编译缓存/参数设置/读回 + 交叉点表（CROSSOVER）+ pick_mode 选路 +
gpu_status 工具。各域 kernel 与其 CPU oracle 已就近迁移：
- `literal_scan` / `byte_hist`（含 entropy）/ `xor_crib_scan` → tools/filescan.py；
- `ngram_bottomk` / `ngram_hashes` / `bottom_k` / `jaccard` → tools/neardupes.py。
公开名不变（域内直呼）；本模块零 kernel，只剩运行时与共享选路。

设计口径（与 LSP/ast-grep 同款"可选外部引擎 + 优雅降级"）：
- **运行时**：`OpenCL.dll`（Windows）/ `libOpenCL.so`（Linux）——系统自带，不装 pip 包；
  NVIDIA 驱动即提供（本机 RTX 4060 Ti / OpenCL 3.0）。CUDA 路线需 nvcc 或 PTX，
  故不采用（nvcc 未装；OpenCL 运行时编译等价且跨厂商）。
- **诚实边界**：GPU 只加速**数据并行**内核（字面量多模式匹配、字节直方图/熵、
  异或密钥爆破、n-gram 指纹）。IO 主导的活（读盘/解析/索引）不因 GPU 变快——
  交叉点由 bench/s114_gpu_bench.py 实测给出，`mode="auto"` 据此选路。
- **降级纪律**：无运行时/无 GPU 设备/内核编译失败 → 明确错误或回落 CPU（不静默、
  不假装）。CPU 参考实现在各域文件（`*_cpu`），既是降级路径也是 GPU 的 oracle。
"""
import ctypes
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


def _read_buf(cl, queue, mem, nbytes):
    out = (ctypes.c_char * nbytes)()
    _check(cl.clEnqueueReadBuffer(queue, mem, 1, 0, nbytes, out, 0, None, None),
           "clEnqueueReadBuffer")
    cl.clFinish(queue)
    return bytes(out)


# ---- 交叉点（bench/s114_gpu_bench.py 实测回填，2026-09-09 / RTX 4060 Ti）----
# 实测：byte_hist GPU 31-38×（1-64MB，预热后，结果与 CPU 逐位一致）；
#       literal_scan GPU 慢 5-6×（CPU bytes.find/memmem 太强）→ auto 恒走 CPU；
#       xor_scan 1MB 0.6× / 4MB 3.8× → 取 2MB 为界；
#       ngram_bottomk 两遍选择 2.1×@8KB→551×@16MB（4KB 时 0.9×，GPU 固定开销 ~4ms）
#       → 交叉点取 8KB；ngram_hashes 全量输出仅 2.8×（带宽受限），只作回退与对拍。
# 换硬件/换数据分布请重跑 bench/s114_gpu_bench.py 再回填。
# S119 退役：dot_matrix（无调用方 + 朴素内核实测 22× 慢于原生 Rust）——见 spec/GPU.md §二
CROSSOVER = {"literal_scan_bytes": 1 << 62, "byte_hist_bytes": 1 << 20,
             "xor_scan_bytes": 2 << 20,
             "ngram_bottomk_bytes": 1 << 13, "ngram_hashes_bytes": 2 << 20}


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
    st["note"] = ("GPU 只加速数据并行内核（直方图/熵/异或枚举/n-gram 指纹等）；"
                  "字面量匹配与 IO/编排类仍走 CPU（交叉点实测见 spec/GPU.md）；"
                  "auto 按实测选路")
    return st
