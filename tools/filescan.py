# -*- coding: utf-8 -*-
"""tools/filescan.py —— 文件扫描（S115）：签名 + 熵启发式 + 哈希 + 异或层枚举。

**定位如实**：签名/启发式扫描器，**不是杀毒软件**——不做行为分析、不做沙箱、
不保证查全；它能做的是：①按给定签名（字面量）匹配；②SHA-256 哈希匹配（黑/白名单）；
③熵/打包启发式（>阈值 + 体积门）标记可疑二进制；④单字节异或层枚举（给已知明文 crib）。
内置 EICAR 测试串（AV 业界标准可验证样例）供自测，不含任何真实恶意样本。

引擎选路（全部实测，见 spec/GPU.md §二 / §二·三）：
- **熵/直方图**：GPU（≥1MB 实测 31-38× 对纯 Python）／CPU。**不迁 Rust**——逐文件调用时
  进程启动（~15ms）≥ 计算本身（16MB 时 Rust 1.4ms+15ms ≈ GPU 16ms；200×4KB 整目录仅 46ms）。
- **异或枚举**：**rust**（`rx-scan xor`，≥256KB 实测 485ms@16MB）→ GPU（128KB-256KB 带
  最优，无进程启动开销）→ CPU 参考（<128KB，C 速度 translate+find）。
- 签名匹配与哈希留在 CPU（bytes.find / hashlib 更快，实测数据在 spec/GPU.md §二）。
"""
import hashlib
import json
import os
import struct
import subprocess

from registry import tool
from tools import gpu
from tools.fs import _resolve as _fs_resolve

_SKIP_DIRS = {".git", "node_modules", "target", "__pycache__", "dist", "build",
              ".venv", "venv", ".pytest_cache"}
_MAX_FILE_BYTES = 256 * 1024 * 1024        # 单文件上限（超过跳过并如实报）
# AV 业界标准测试串（EICAR）——可验证"签名匹配确实工作"，不含真实恶意样本
_EICAR = (b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*")

_RX_SCAN_EXE_NAME = "rx-scan.exe"
# 实测交叉点（单文件 / min-of-3，2026-09-09）：
#   64KB  cpu 14.5ms < rust 17.8ms        → CPU
#   128KB gpu 12.9ms < rust 19.2ms        → GPU
#   256KB gpu 22.9ms ≈ rust 23.7ms        → 分界
#   512KB rust 32.2ms < gpu 43.5ms        → Rust
#   16MB  rust 485ms  < gpu 1279ms        → Rust（3× 吞吐差）
_XOR_RUST_MIN = 256 * 1024
_XOR_GPU_MIN = 128 * 1024


def _walk(root, max_files):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in sorted(filenames):
            out.append(os.path.join(dirpath, fn))
            if len(out) >= max_files:
                return out
    return out


def _rx_scan_exe():
    """定位 rx-scan.exe：UNIFIED_RX_RS_EXE 覆盖 → cargo 目标目录惯例路径。

    与 tools/scan.py::_rx_scan_exe 同纪律：候选必须已存在且文件名恰为
    rx-scan.exe（argv 固定前缀、list 形式、无 shell）。
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


def _rust_xor(fp, crib):
    """单文件异或枚举走 rx-scan.exe xor（路径经 stdin 帧流）。

    返回 [(key, count)]；exe 缺失/超时/非 JSON → ValueError（调用方按交叉点回落）。
    """
    exe = _rx_scan_exe()
    if not exe:
        raise ValueError("rx-scan.exe 不存在——先在 rust/ 下 cargo build --release "
                         "（或设 UNIFIED_RX_RS_EXE 指向现有 exe）")
    p = fp.encode("utf-8")
    buf = struct.pack("<I", len(p)) + p + struct.pack("<I", 0)
    try:
        cp = subprocess.run([exe, "xor", crib.hex()], capture_output=True,
                            timeout=300, input=buf)
    except subprocess.TimeoutExpired:
        raise ValueError("rx-scan xor 超时（300s）")
    tail = (cp.stderr or b"").decode("utf-8", errors="replace").strip()[-300:]
    lines = (cp.stdout or b"").decode("utf-8", errors="replace").strip().splitlines()
    if not lines:
        raise ValueError(f"rx-scan xor 无输出（exit={cp.returncode}）: {tail}")
    try:
        out = json.loads(lines[-1])
    except ValueError:
        raise ValueError(f"rx-scan xor 输出非 JSON: {lines[-1][:200]}")
    if cp.returncode != 0 or not isinstance(out, dict) or "files" not in out:
        raise ValueError(f"rx-scan xor 失败（exit={cp.returncode}）: {tail or lines[-1][:200]}")
    for ent in out["errors"]:
        raise ValueError(f"rx-scan xor 读取失败: {ent.get('path')}（{ent.get('error')}）")
    if not out["files"]:
        return []
    return [(int(k), int(c)) for k, c in out["files"][0].get("keys", [])]


def _xor_scan(fp, data, crib, engine):
    """单字节异或枚举 → ([(key,count)], 引擎, 回落原因)。

    选路（实测交叉点，见文件头）：auto 下 rust（≥256KB）→ GPU（≥128KB）→ CPU；
    强制 engine 不可用时按交叉点回落并**如实回传原因**（不静默降级）。
    """
    n = len(data)
    fallback = None
    if engine in ("auto", "rust") and (engine == "rust" or n >= _XOR_RUST_MIN):
        try:
            return _rust_xor(fp, crib), "rust", None
        except ValueError as e:
            fallback = str(e)
            if engine == "rust":
                engine = "auto"
    if engine in ("auto", "gpu") and (engine == "gpu" or n >= _XOR_GPU_MIN):
        try:
            return gpu.xor_crib_scan_gpu(data, crib), "gpu", fallback
        except gpu.GpuError as e:
            fallback = (fallback + "；" if fallback else "") + f"GPU 不可用: {e}"
    return gpu.xor_crib_scan_cpu(data, crib), "cpu", fallback


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


@tool("file_scan", "文件扫描（签名/熵启发式/哈希/异或层；非杀毒软件）：字面量签名 + "
      "SHA-256 哈希比对 + 打包熵检测（GPU）+ 单字节异或枚举（rust/GPU/CPU 三档如实上报）",
      "scan",
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
           "xor_engine": {"type": "string", "enum": ["auto", "rust", "gpu", "cpu"],
                          "description": "异或枚举引擎（默认 auto：≥256KB rust → ≥128KB gpu → cpu）"},
           "max_files": {"type": "integer", "description": "扫描文件上限（默认 200）"},
           "xor_crib": {"type": "string",
                        "description": "单字节异或层探测的已知明文（≥6 字节；二进制用 "
                                       "hex:4d5a900003000000 形式；给则启用）"},
       },
       "required": ["path"]})
def file_scan(path, signatures=None, hashes=None, entropy_threshold=7.0,
              engine="auto", max_files=200, xor_crib=None, xor_engine="auto"):
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
    crib = None
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

    files = [path] if os.path.isfile(path) else _walk(path, int(max_files))
    findings, skipped, engines = [], [], {"gpu": 0, "cpu": 0}
    xor_engines, xor_fallback = {"rust": 0, "gpu": 0, "cpu": 0}, None
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
        xor_keys, xor_used = [], None
        if crib:
            pairs, xor_used, fb = _xor_scan(fp, data, crib, xor_engine)
            if fb and not xor_fallback:
                xor_fallback = fb
            xor_engines[xor_used] = xor_engines.get(xor_used, 0) + 1
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
                             "packed": packed, "xor_keys": xor_keys,
                             "xor_engine": xor_used})
    return {"path": path, "scanned": len(files), "findings": findings,
            "skipped": skipped[:20], "entropy_engine": engines,
            "xor_engine": xor_engines, "xor_fallback": xor_fallback,
            "note": "签名/熵启发式扫描，**非杀毒软件**（无行为分析/沙箱/查全保证）；"
                    "默认签名仅含 EICAR 测试串；packed=熵>阈值且≥4KB（打包/加密启发式，"
                    "会误报已压缩资源）。xor_keys=单字节异或层枚举（需给 xor_crib "
                    "已知明文（≥6 字节，否则高熵数据上噪声淹没真密钥）；多字节异或不做）。"
                    "熵走 GPU；异或枚举 rust（≥256KB）→ GPU（≥128KB）→ CPU，见 xor_engine"}
