"""test_ide_explore_search.py — 探索/搜索维接线测试（2026-08-13）。

覆盖：
  1. explore_code：LATS 探索（目标词 → 树搜索，返回 best + 候选）
  2. semantic_search：全库 BM25 检索（英文 + 中文 bigram 切词）
  3. CJK 切词函数
"""
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

ROOT = r"D:\开发\VoxelForge-Nexus"


def test_explore_code():
    r = server._call("explore_code", {"root": ROOT, "goal": "physics 驱动 车轮", "budget": 10})
    d = json.loads(r[0].text)
    assert d.get("best"), f"应返回最佳节点: {d.get('error', '')}"
    assert d.get("candidates_found", 0) > 0
    assert "tree_size" in d


def test_semantic_search_english():
    r = server._call("semantic_search", {"root": ROOT, "query": "physics vehicle", "limit": 3})
    d = json.loads(r[0].text)
    assert d.get("ok") is True
    assert len(d.get("results", [])) > 0


def test_semantic_search_chinese():
    """中文 bigram 检索：车轮 → wheels.rs。"""
    r = server._call("semantic_search", {"root": ROOT, "query": "车轮 驱动", "limit": 5})
    d = json.loads(r[0].text)
    assert d.get("ok") is True
    results = d.get("results", [])
    assert len(results) > 0
    ids = " ".join(str(x.get("id", "")) for x in results)
    assert "wheels" in ids or "physics_drive" in ids, f"应命中轮子相关: {ids[:120]}"


def test_cjk_space_content():
    out = server._cjk_space_content("车轮驱动")
    assert out == "车 轮 驱 动 ", repr(out)


def test_cjk_query():
    out = server._cjk_bigram_query("车轮 驱动")
    assert "车 轮" in out and "驱 动" in out, repr(out)


def test_cleanup_index():
    """清理测试索引（避免污染 VoxelForge）。"""
    shutil.rmtree(os.path.join(ROOT, ".unified-rx-index"), ignore_errors=True)
