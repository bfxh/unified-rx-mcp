# -*- coding: utf-8 -*-
"""tools/filescan.py —— 文件扫描（S115）：签名 + 熵启发式 + 哈希（GPU 加速熵计算）。

**定位如实**：签名/启发式扫描器，**不是杀毒软件**——不做行为分析、不做沙箱、
不保证查全；它能做的是：①按给定签名（字面量）匹配；②SHA-256 哈希匹配（黑/白名单）；
③熵/打包启发式（>阈值 + 体积门）标记可疑二进制。内置 EICAR 测试串（AV 业界标准
可验证样例）供自测，不含任何真实恶意样本。

GPU 接法（见 spec/GPU.md 实测）：熵/直方图走 `tools/gpu.py` 的 OpenCL 内核
（≥1MB 时实测 31-38×，结果与 CPU 逐位一致）；签名匹配与哈希留在 CPU
（CPU 的 bytes.find/hashlib 更快，实测数据在 spec/GPU.md §二）。
"""
import hashlib
import os

from registry import tool
from tools import gpu
from tools.fs import _resolve as _fs_resolve

_SKIP_DIRS = {".git", "node_modules", "target", "__pycache__", "dist", "build",
              ".venv", "venv", ".pytest_cache"}
_MAX_FILE_BYTES = 256 * 1024 * 1024        # 单文件上限（超过跳过并如实报）
# AV 业界标准测试串（EICAR）——可验证"签名匹配确实工作"，不含真实恶意样本
_EICAR = (b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*")


def _walk(root, max_files):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in sorted(filenames):
            out.append(os.path.join(dirpath, fn))
            if len(out) >= max_files:
                return out
    return out


def _entropy_of(data, engine):
    """熵：≥1MB 且 GPU 可用时走 GPU 直方图（auto 按实测交叉点选路）。"""
    mode = gpu.pick_mode("byte_hist_bytes", len(data), engine)
    if mode == "gpu":
        try:
            hist = gpu.byte_hist_gpu(data)
            return gpu.entropy(hist, len(data)), "gpu"
        except gpu.GpuError:
            pass                     # 明确降级到 CPU（gpu_status 可查原因）
    hist = gpu.byte_hist_cpu(data)
    return gpu.entropy(hist, len(data)), "cpu"


@tool("file_scan", "文件扫描（签名/熵启发式/哈希；非杀毒软件）：字面量签名匹配 + "
      "SHA-256 哈希比对 + 打包熵检测——熵计算走 GPU（≥1MB 实测 31-38×）", "scan",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "文件或目录（沙盒内）"},
           "signatures": {"type": "array", "items": {"type": "string"},
                          "description": "字面量签名（UTF-8；默认只含 EICAR 测试串）"},
           "hashes": {"type": "array", "items": {"type": "string"},
                      "description": "SHA-256 黑名单（小写十六进制；命中即报）"},
           "entropy_threshold": {"type": "number",
                                 "description": "打包判定熵阈值（默认 7.0；>阈值且 ≥4KB 标 packed）"},
           "engine": {"type": "string", "enum": ["auto", "cpu", "gpu"],
                      "description": "熵计算引擎（默认 auto：按实测交叉点选路）"},
           "max_files": {"type": "integer", "description": "扫描文件上限（默认 200）"},
       },
       "required": ["path"]})
def file_scan(path, signatures=None, hashes=None, entropy_threshold=7.0,
              engine="auto", max_files=200):
    try:
        path = _fs_resolve(path)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.exists(path):
        return {"error": f"路径不存在: {path}"}
    sigs = [_EICAR]
    for s in (signatures or []):
        if isinstance(s, str) and s:
            sigs.append(s.encode("utf-8", "replace"))
    bl = {h.strip().lower() for h in (hashes or []) if isinstance(h, str) and h.strip()}

    files = [path] if os.path.isfile(path) else _walk(path, int(max_files))
    findings, skipped, engines = [], [], {"gpu": 0, "cpu": 0}
    for fp in files:
        try:
            size = os.path.getsize(fp)
        except OSError:
            continue
        if size > _MAX_FILE_BYTES:
            skipped.append({"file": fp, "reason": f"超过单文件上限（{size} 字节）"})
            continue
        try:
            with open(fp, "rb") as f:
                data = f.read()
        except OSError as e:
            skipped.append({"file": fp, "reason": f"读取失败: {e}"})
            continue
        sha = hashlib.sha256(data).hexdigest()
        hit_sigs = [s.decode("utf-8", "replace") for s in sigs if s and s in data]
        hit_hash = sha in bl
        ent, used = _entropy_of(data, engine)
        engines[used] = engines.get(used, 0) + 1
        packed = bool(size >= 4096 and ent > float(entropy_threshold))
        if hit_sigs or hit_hash or packed:
            findings.append({"file": fp, "size": size, "sha256": sha,
                             "entropy": round(ent, 3), "engine": used,
                             "signatures": hit_sigs, "hash_hit": hit_hash,
                             "packed": packed})
    return {"path": path, "scanned": len(files), "findings": findings,
            "skipped": skipped[:20], "entropy_engine": engines,
            "note": "签名/熵启发式扫描，**非杀毒软件**（无行为分析/沙箱/查全保证）；"
                    "默认签名仅含 EICAR 测试串；packed=熵>阈值且≥4KB（打包/加密启发式，"
                    "会误报已压缩资源）。GPU 只加速熵计算（spec/GPU.md 实测）"}
