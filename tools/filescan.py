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
           "xor_crib": {"type": "string",
                        "description": "单字节异或层探测的已知明文（≥6 字节；二进制用 "
                                       "hex:4d5a900003000000 形式；给则启用）"},
       },
       "required": ["path"]})
def file_scan(path, signatures=None, hashes=None, entropy_threshold=7.0,
              engine="auto", max_files=200, xor_crib=None):
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
        xor_keys = []
        if xor_crib:
            # 二进制 crib 用 hex: 形式（字符串经 UTF-8 重编码会改变字节——实测坑）
            if xor_crib.startswith("hex:"):
                try:
                    crib = bytes.fromhex(xor_crib[4:].strip())
                except ValueError:
                    return {"error": "xor_crib hex 解析失败（示例 hex:4d5a900003000000）"}
            else:
                crib = xor_crib.encode("utf-8", "replace")
            if len(crib) < 4:
                # 短 crib 在高熵数据上每个密钥都会随机命中（N/256^L 次）——如实拒绝
                return {"error": f"xor_crib 太短（{len(crib)} 字节）：高熵数据上短 crib "
                                 f"每个密钥都会随机命中，无法区分真密钥；请给 ≥6 字节"
                                 f"的已知明文（如 PE 头 8 字节）"}
            xmode = gpu.pick_mode("xor_scan_bytes", len(data), engine)
            try:
                pairs = (gpu.xor_crib_scan_gpu(data, crib) if xmode == "gpu"
                         else gpu.xor_crib_scan_cpu(data, crib))
            except gpu.GpuError:
                pairs = gpu.xor_crib_scan_cpu(data, crib)
            xor_keys = [{"key": k, "count": c} for k, c in pairs[:8]]
            # 真密钥通常 count 小、随机密钥 count≈N/256^L；crib≥6 字节时噪声趋零
        hit_sigs = [s.decode("utf-8", "replace") for s in sigs if s and s in data]
        hit_hash = sha in bl
        ent, used = _entropy_of(data, engine)
        engines[used] = engines.get(used, 0) + 1
        packed = bool(size >= 4096 and ent > float(entropy_threshold))
        if hit_sigs or hit_hash or packed or xor_keys:
            findings.append({"file": fp, "size": size, "sha256": sha,
                             "entropy": round(ent, 3), "engine": used,
                             "signatures": hit_sigs, "hash_hit": hit_hash,
                             "packed": packed, "xor_keys": xor_keys})
    return {"path": path, "scanned": len(files), "findings": findings,
            "skipped": skipped[:20], "entropy_engine": engines,
            "note": "签名/熵启发式扫描，**非杀毒软件**（无行为分析/沙箱/查全保证）；"
                    "默认签名仅含 EICAR 测试串；packed=熵>阈值且≥4KB（打包/加密启发式，"
                    "会误报已压缩资源）。xor_keys=单字节异或层枚举（需给 xor_crib "
                    "已知明文（≥6 字节，否则高熵数据上噪声淹没真密钥）；多字节异或不做）。"
                    "GPU 加速熵与异或枚举（spec/GPU.md 实测）"}
