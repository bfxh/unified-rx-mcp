# -*- coding: utf-8 -*-
"""S101 code_search hybrid：BM25（行级）× code_semantic（定义级）RRF 融合契约。

背景：两路互补——BM25 强在词面命中，语义路强在"定义级"聚合。RRF
（Reciprocal Rank Fusion，k=60，免分数归一化）融合后，两路都命中的条目上浮
（实测 `sandbox resolve` 查询：语义路 #1 / BM25 #3 的 `_resolve_in_sandbox`
被顶到第一，压过只被 BM25 命中的注释行）。

契约：
- hybrid=false（默认）→ 输出与旧版逐字段同形（无 hybrid/paths 键）；
- hybrid=true → {hybrid, rrf_k, paths{bm25,semantic}, hits[{file,line,rrf,
  bm25_rank,semantic_rank,symbol?,kind?,snippet?}]}；
- 语义路不可用（exe 缺失/工具级错误）→ **显式降级**：返回 BM25 结果 +
  hybrid=false + degraded 原因，不静默、不报错。
"""
import os
import sys

import pytest

import registry
import tools  # noqa: F401
from tools import search as search_mod


def _hit(file, line, **extra):
    h = {"file": file, "line": line}
    h.update(extra)
    return h


def test_rrf_fuse_math_and_merge():
    bm25 = {"hits": [_hit("a.py", 1), _hit("b.py", 2, snippet="B 行")]}
    sem = {"hits": [_hit("b.py", 2, symbol="b_func", kind="def", snippet="def b_func"),
                    _hit("c.py", 3, symbol="c_func")]}
    out = search_mod._rrf_fuse("q", bm25, sem, 10)
    assert out["hybrid"] is True and out["rrf_k"] == 60
    assert out["paths"] == {"bm25": 2, "semantic": 2}
    # b.py 两路都中（rank2/rank1）→ 必居首
    top = out["hits"][0]
    assert top["file"] == "b.py" and top["line"] == 2
    assert top["bm25_rank"] == 2 and top["semantic_rank"] == 1
    assert abs(top["rrf"] - (1 / 62 + 1 / 61)) < 1e-6
    # 字段合并：语义路的 symbol/kind 与 snippet 并入
    assert top["symbol"] == "b_func" and top["kind"] == "def"
    assert top["snippet"] == "def b_func"
    # 单路条目 rank 另一侧为 None
    a = [h for h in out["hits"] if h["file"] == "a.py"][0]
    assert a["semantic_rank"] is None and a["bm25_rank"] == 1


def test_default_shape_unchanged(tmp_path):
    (tmp_path / "m.py").write_text("def target_func():\n    return 1\n", encoding="utf-8")
    r = search_mod.code_search("target_func", root=str(tmp_path))
    assert "error" not in r, r
    assert "hybrid" not in r and "paths" not in r, r
    assert r["total"] >= 1 and "hits" in r


@pytest.mark.skipif(search_mod._rx_semantic_exe() is None,
                    reason="rx-semantic.exe 缺失")
def test_hybrid_end_to_end(tmp_path):
    (tmp_path / "defs.py").write_text(
        "def sandbox_resolve(path):\n    return path\n", encoding="utf-8")
    (tmp_path / "noise.py").write_text(
        "# sandbox and resolve are mentioned here\n", encoding="utf-8")
    r = registry.call("code_search", {"query": "sandbox resolve",
                                      "root": str(tmp_path), "k": 5,
                                      "hybrid": True})
    assert r.get("ok"), r
    res = r["result"]
    assert res["hybrid"] is True and res["rrf_k"] == 60
    assert res["paths"]["bm25"] >= 1 and res["paths"]["semantic"] >= 1
    top = res["hits"][0]
    assert top["file"].endswith("defs.py"), res["hits"]
    assert top["symbol"] == "sandbox_resolve"
    assert top["semantic_rank"] is not None and top["bm25_rank"] is not None


def test_hybrid_degrades_explicitly(tmp_path, monkeypatch):
    (tmp_path / "m.py").write_text("def keep_me():\n    return 1\n", encoding="utf-8")

    def boom(*a, **k):
        raise ValueError("rx-semantic.exe 不存在")

    monkeypatch.setattr(search_mod, "_rx_semantic_call", boom)
    r = search_mod.code_search("keep_me", root=str(tmp_path), hybrid=True)
    assert "error" not in r, r
    assert r["hybrid"] is False
    assert "语义路不可用" in r["degraded"]
    assert r["total"] >= 1, "降级后 BM25 结果必须完整保留"
