# -*- coding: utf-8 -*-
"""S117 近似重复聚类契约：bottom-k MinHash 两遍选择引擎 + near_dupes 工具。

三条口径入册（实测见 spec/GPU.md §二）：
① 直方图余弦对高熵数据无区分力（随机文件相似度 0.98）→ 改 bottom-k MinHash；
② 全量哈希输出受带宽限制仅 2.8× → 两遍选择（直方图定阈值 + 按阈值发射）
   把回传量从 O(n) 降到 O(k)，GPU vs CPU 参考 1.7-551×；
③ 单桶超容量上限的极端分布（全零数据）自动回退全量路径，结果口径不变。
"""
import os

import pytest

import registry
import tools  # noqa: F401
from tools import gpu
from tools import neardupes  # noqa: F401

_HAS_GPU = gpu.status().get("available") is True


def _fnv1a32_independent(data, ng):
    """测试侧独立实现（常量写成十六进制、逐字节手算，不复用被测代码）。"""
    out = []
    for i in range(max(0, len(data) - ng + 1)):
        h = 0x811C9DC5
        for b in data[i:i + ng]:
            h = ((h ^ b) * 0x01000193) % (1 << 32)
        out.append(h)
    return out


def test_bottom_k_and_jaccard_units():
    assert gpu.bottom_k([], 8) == frozenset()
    # 取 k 个最小，重复值只算一个（CPU/GPU 两路同口径）
    assert gpu.bottom_k([5, 1, 9, 1, 3], 2) == frozenset({1})
    assert gpu.bottom_k([5, 1, 9, 1, 3], 3) == frozenset({1, 3})
    assert gpu.bottom_k([5, 1, 9], 99) == frozenset({1, 5, 9})
    assert gpu.jaccard(frozenset(), frozenset()) == 0.0
    assert gpu.jaccard(frozenset({1, 2}), frozenset({1, 2})) == 1.0
    assert gpu.jaccard(frozenset({1, 2}), frozenset({3, 4})) == 0.0
    assert gpu.jaccard(frozenset({1, 2}), frozenset({2, 3})) == pytest.approx(1 / 3)


def test_fnv_cpu_matches_independent_oracle():
    data = os.urandom(4096)
    for ng in (2, 4, 7):
        assert gpu.ngram_hashes_cpu(data, ng) == _fnv1a32_independent(data, ng), ng


def test_bottomk_cpu_matches_bruteforce():
    data = os.urandom(4096)
    for ng, k in ((4, 128), (3, 16)):
        allh = _fnv1a32_independent(data, ng)
        want = frozenset(sorted(allh)[:k])
        assert gpu.ngram_bottomk_cpu(data, ng, k) == want, (ng, k)
    # 短于 n-gram / 空数据：空指纹（不抛穿）
    assert gpu.ngram_bottomk_cpu(b"ab", 4, 8) == frozenset()


def test_pick_mode_bottomk_crossover():
    assert gpu.pick_mode("ngram_bottomk_bytes", 4 << 10) == "cpu"
    assert gpu.pick_mode("ngram_bottomk_bytes", 1 << 20) == "gpu"
    assert gpu.pick_mode("ngram_bottomk_bytes", 4 << 10, mode="gpu") == "gpu"


@pytest.mark.skipif(not _HAS_GPU, reason="本机无 GPU/OpenCL")
def test_bottomk_gpu_matches_cpu_oracle():
    for n in (9, 5000, 300_000):
        data = os.urandom(n)
        for ng, k in ((4, 128), (2, 16)):
            g = gpu.ngram_bottomk_gpu(data, ng, k)
            c = gpu.ngram_bottomk_cpu(data, ng, k)
            assert g == c, (n, ng, k, len(g), len(c))
            assert g <= set(_fnv1a32_independent(data, ng)), "指纹必须是真实哈希子集"


@pytest.mark.skipif(not _HAS_GPU, reason="本机无 GPU/OpenCL")
def test_bottomk_gpu_overflow_falls_back():
    # 全零数据：所有 n-gram 哈希相同 → 单桶计数 = n 远超容量上限 → 回退全量路径
    data = bytes(200_000)
    want = frozenset({_fnv1a32_independent(bytes(4), 4)[0]})
    assert gpu.ngram_bottomk_gpu(data, 4, 128) == gpu.ngram_bottomk_cpu(data, 4, 128)
    assert gpu.ngram_bottomk_gpu(data, 4, 128) == want
    assert gpu.ngram_bottomk_gpu(b"", 4, 8) == frozenset()


@pytest.mark.skipif(not _HAS_GPU, reason="本机无 GPU/OpenCL")
def test_ngram_hashes_gpu_matches_cpu_oracle():
    data = os.urandom(50_000)
    assert gpu.ngram_hashes_gpu(data, 4) == gpu.ngram_hashes_cpu(data, 4)


def _mk_pair(tmp_path):
    a = os.urandom(200_000)
    b = bytearray(a)
    b[100_000:100_008] = os.urandom(8)          # 原地改 8 字节 → 近重复
    (tmp_path / "a.bin").write_bytes(a)
    (tmp_path / "b.bin").write_bytes(bytes(b))
    (tmp_path / "r.bin").write_bytes(os.urandom(200_000))
    return tmp_path


def test_near_dupes_clusters_near_duplicate_excludes_random(tmp_path):
    _mk_pair(tmp_path)
    r = registry.call("near_dupes", {"path": str(tmp_path)})
    assert r.get("ok"), r
    res = r["result"]
    clusters = [sorted(c) for c in res["clusters"]]
    assert clusters == [[str(tmp_path / "a.bin"), str(tmp_path / "b.bin")]], clusters
    assert all(p["similarity"] >= 0.8 for p in res["pairs"]), res["pairs"]


def test_near_dupes_cpu_engine_all_cpu(tmp_path):
    _mk_pair(tmp_path)
    r = registry.call("near_dupes", {"path": str(tmp_path), "engine": "cpu"})
    assert r.get("ok"), r
    assert r["result"]["sketch_engine"] == {"rust": 0, "gpu": 0, "cpu": 3}, \
        r["result"]["sketch_engine"]


def test_near_dupes_sandbox_reject(tmp_path, monkeypatch):
    _mk_pair(tmp_path)
    monkeypatch.setenv("UNIFIED_RX_SANDBOX", str(tmp_path / "inside-only"))
    r = registry.call("near_dupes", {"path": str(tmp_path)})
    assert r.get("ok") is False, r
    assert "沙盒" in r["error"] or "越界" in r["error"], r


def test_near_dupes_skips_oversized(tmp_path):
    (tmp_path / "x.bin").write_bytes(os.urandom(1024))
    r = registry.call("near_dupes", {"path": str(tmp_path), "max_file_mb": 0})
    assert r.get("ok"), r
    res = r["result"]
    assert res["files"] == 0 and res["skipped"][0]["reason"] == "超过单文件上限", res
