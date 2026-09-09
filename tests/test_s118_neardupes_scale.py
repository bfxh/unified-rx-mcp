# -*- coding: utf-8 -*-
"""S118 near_dupes 规模化契约：精确候选剪枝（不丢真对）+ 截断如实上报。

背景（实测）：两两比较原为 O(n²) 全对——100 文件 263ms、300 文件 1033ms；
且 `max_files` 截断**静默**发生（结果看起来像全量）。本轮：
- 倒排索引 + Jaccard 下界 I ≥ 2tm/(1+t) 剪枝（下界对每对成立 → 精确）；
- `walk_truncated` 标记截断。
"""
import os

import pytest

import registry
import tools  # noqa: F401
from tools import gpu, neardupes


def _jacc(a, b):
    return gpu.jaccard(a, b)


def test_candidate_pruning_never_misses_true_pair():
    """随机指纹集合：凡 Jaccard ≥ t 的对必须在候选里（下界剪枝的精确性）。"""
    import random
    random.seed(11)
    vecs = [frozenset(random.randrange(0, 5000) for _ in range(128)) for _ in range(60)]
    # 造几组近重复（复制后改 2 个元素）
    for src in (0, 5, 20):
        v = set(vecs[src])
        v.discard(min(v))
        v.add(99999)
        vecs.append(frozenset(v))
    for t in (0.5, 0.8, 0.95):
        cand, _ = neardupes._candidate_pairs(vecs, t)
        cand = set(cand)
        for i in range(len(vecs)):
            for j in range(i + 1, len(vecs)):
                if _jacc(vecs[i], vecs[j]) >= t:
                    assert (i, j) in cand or (j, i) in cand, (t, i, j)


def test_candidate_pruning_degenerate_short_fingerprints():
    """指纹极短（m→0）→ 退化为全对比较，不丢对、不崩。"""
    vecs = [frozenset({1}), frozenset({1}), frozenset(), frozenset({2})]
    cand, shared = neardupes._candidate_pairs(vecs, 0.8)
    assert set(cand) == {(0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3)}, cand
    assert shared == 6


def _mk_corpus(tmp_path):
    a = os.urandom(60_000)
    b = bytearray(a)
    b[30_000:30_008] = os.urandom(8)
    (tmp_path / "a.bin").write_bytes(a)
    (tmp_path / "b.bin").write_bytes(bytes(b))
    (tmp_path / "r1.bin").write_bytes(os.urandom(60_000))
    (tmp_path / "r2.bin").write_bytes(os.urandom(60_000))
    return tmp_path


def test_pruned_result_equals_brute_force(tmp_path, monkeypatch):
    """剪枝路径 vs 全对暴力路径：pairs / clusters 逐项相等（精确，不是近似）。"""
    _mk_corpus(tmp_path)
    pruned = registry.call("near_dupes", {"path": str(tmp_path), "engine": "cpu"})
    assert pruned.get("ok"), pruned

    def _brute(vecs, threshold):
        n = len(vecs)
        return [(i, j) for i in range(n) for j in range(i + 1, n)], n * (n - 1) // 2

    monkeypatch.setattr(neardupes, "_candidate_pairs", _brute)
    brute = registry.call("near_dupes", {"path": str(tmp_path), "engine": "cpu"})
    assert brute.get("ok"), brute
    assert pruned["result"]["pairs"] == brute["result"]["pairs"]
    assert pruned["result"]["clusters"] == brute["result"]["clusters"]


def test_walk_truncated_reported(tmp_path):
    for i in range(5):
        (tmp_path / f"f{i}.bin").write_bytes(os.urandom(2000))
    r3 = registry.call("near_dupes", {"path": str(tmp_path), "max_files": 3,
                                      "engine": "cpu"})
    assert r3.get("ok") and r3["result"]["files"] == 3
    assert r3["result"]["walk_truncated"] is True, r3["result"]
    r9 = registry.call("near_dupes", {"path": str(tmp_path), "max_files": 9,
                                      "engine": "cpu"})
    assert r9["result"]["walk_truncated"] is False, r9["result"]


def test_random_corpus_candidates_pruned(tmp_path):
    """全随机语料：几乎无共享指纹 → 候选数远小于全对数（剪枝生效）。"""
    for i in range(30):
        (tmp_path / f"r{i:02d}.bin").write_bytes(os.urandom(20_000))
    r = registry.call("near_dupes", {"path": str(tmp_path), "engine": "cpu"})
    assert r.get("ok"), r
    res = r["result"]
    assert res["files"] == 30
    assert res["candidates"] < 435 // 4, res  # 全对 = C(30,2) = 435
    assert res["pairs"] == [], res["pairs"]
