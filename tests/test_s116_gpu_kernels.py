# -*- coding: utf-8 -*-
"""S116 GPU 内核扩面契约：异或密钥枚举 / 点积矩阵（vs CPU oracle）+ file_scan 接入。

两个实测坑入册：
① 内核计数封顶后 early-return 会跳过写回（GPU/CPU 结果不一致）→ 改饱和计数恒写回；
② 二进制 crib 经 UTF-8 重编码会改变字节 → 支持 `hex:` 形式（否则真密钥永远找不到）。
"""
import os

import pytest

import registry
import tools  # noqa: F401
from tools import gpu

_HAS_GPU = gpu.status().get("available") is True


def test_pick_mode_new_kernels():
    assert gpu.pick_mode("xor_scan_bytes", 1 << 20) == "cpu"
    assert gpu.pick_mode("xor_scan_bytes", 4 << 20) == "gpu"
    assert gpu.pick_mode("dot_matrix_flops", 70_000) == "cpu"
    assert gpu.pick_mode("dot_matrix_flops", 1_000_000) == "gpu"


def test_xor_cpu_reference_saturates():
    # 饱和口径：同一 crib 出现 >256 次也只记 256（GPU 内核同口径）。
    # 注："MZ"*400 对 key=0 与 key=0x17（M^Z）都命中——两者都应封顶 256。
    data = b"MZ" * 400
    hits = dict(gpu.xor_crib_scan_cpu(data, b"MZ"))
    assert hits.get(0) == 256 and hits.get(0x17) == 256, hits
    assert all(v <= 256 for v in hits.values()), hits


@pytest.mark.skipif(not _HAS_GPU, reason="本机无 GPU/OpenCL")
def test_xor_gpu_matches_cpu_oracle():
    crib = b"MZ\x90\x00\x03\x00\x00\x00"
    data = bytes(b ^ 0x5A for b in (crib + os.urandom(1 << 20)))
    g = gpu.xor_crib_scan_gpu(data, crib)
    c = gpu.xor_crib_scan_cpu(data, crib)
    assert g == c, (g[:5], c[:5])
    assert g == [(0x5A, 1)], f"应唯一锁定真密钥 0x5A: {g[:5]}"


@pytest.mark.skipif(not _HAS_GPU, reason="本机无 GPU/OpenCL")
def test_dot_matrix_gpu_matches_cpu_oracle():
    m, k, n = 64, 128, 64
    a = [0.001 * (i % 97) for i in range(m * k)]
    b = [0.001 * (i % 89) for i in range(k * n)]
    g = gpu.dot_matrix_gpu(a, b, m, k, n)
    c = gpu.dot_matrix_cpu(a, b, m, k, n)
    mx = max(abs(x) for x in c) or 1.0
    md = max(abs(x - y) for x, y in zip(g, c))
    assert md / mx < 1e-4, f"float32 vs float64 相对误差过大: {md/mx}"


def _mk_obf(tmp_path, key=0x5A, crib=b"MZ\x90\x00\x03\x00\x00\x00", n=1 << 20):
    payload = bytes(b ^ key for b in (crib + os.urandom(n)))
    (tmp_path / "obf.bin").write_bytes(payload)
    return crib


def test_file_scan_xor_hex_crib_finds_key(tmp_path):
    _mk_obf(tmp_path)
    r = registry.call("file_scan", {"path": str(tmp_path),
                                    "xor_crib": "hex:4d5a900003000000"})
    assert r.get("ok"), r
    f = r["result"]["findings"][0]
    assert f["xor_keys"] == [{"key": 0x5A, "count": 1}], f


def test_file_scan_xor_short_crib_refused(tmp_path):
    _mk_obf(tmp_path)
    r = registry.call("file_scan", {"path": str(tmp_path), "xor_crib": "MZ"})
    assert r.get("ok") is False and "太短" in r["error"], r


def test_file_scan_xor_clean_file_no_keys(tmp_path):
    (tmp_path / "clean.txt").write_text("no obfuscation here\n", encoding="utf-8")
    r = registry.call("file_scan", {"path": str(tmp_path),
                                    "xor_crib": "hex:4d5a900003000000"})
    assert r.get("ok"), r
    assert r["result"]["findings"] == [], r
