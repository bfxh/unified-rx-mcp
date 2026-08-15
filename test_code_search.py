# -*- coding: utf-8 -*-
"""code_search 测试（阶段4：Rust 语义检索工具 + explore 兜底）。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import search_core  # noqa: E402
import server  # noqa: E402


def _vf2_root():
    return r"D:\开发\VoxelForge-Nexus\vf2"


def test_search_core_roundtrip():
    """桥接：索引 + 语义检索（中文与符号）。"""
    out = search_core.search("命中盒计算", _vf2_root(), k=5)
    assert out is not None and out["ok"] is True
    assert out["count"] >= 1
    paths = [h["path"] for h in out["hits"]]
    assert any("input.rs" in p for p in paths), paths
    # 符号查询 → 行号定位
    out2 = search_core.search("placement_target", _vf2_root(), k=5)
    assert out2["count"] >= 1
    top = out2["hits"][0]
    assert top["line"] > 0, "符号查询应定位行号"
    search_core.shutdown()


def test_code_search_tool_registered():
    assert "code_search" in server._TOOLS
    # 旧 semantic_search 保留（不冲突）
    assert "semantic_search" in server._TOOLS


def test_code_search_tool_call():
    r = json.loads(server._call("code_search", {
        "query": "命中盒", "root": _vf2_root(), "k": 3})[0].text)
    assert r["ok"] is True
    assert r["count"] >= 1


def test_explore_fallback():
    """explore_code 关键词失败 → semantic_fallback 兜底。"""
    r = json.loads(server._call("explore_code", {
        "root": _vf2_root(), "goal": "zzz绝对不存在的词xyz"})[0].text)
    assert r.get("ok") is True
    assert r.get("mode") == "semantic_fallback"


# 召回率评测集（VoxelForge-Nexus/vf2 真实语料）：查询 → 预期文件
_RECALL_QUERIES = [
    ("放置模块时命中盒计算的函数", ["input.rs", "render_bridge.rs"]),
    ("placement_target", ["input.rs"]),
    ("旋转 24 种朝向 姿态", ["rotation.rs"]),
    ("左键拿起 放置 状态机", ["input_systems.rs", "input.rs"]),
    ("模块 装配 增删 查询", ["assembly.rs"]),
    ("渲染 实体 网格 相机", ["main.rs", "render_bridge.rs"]),
]


def test_recall_rate():
    """召回率评测：6 查询 top3 全部命中预期文件（真实语料）。"""
    out = search_core.search("预热", _vf2_root(), k=1)  # 触发索引
    assert out is not None
    for q, expected in _RECALL_QUERIES:
        r = search_core.search(q, _vf2_root(), k=5)
        assert r is not None, f"查询失败: {q}"
        top3 = [h["path"].replace("\\", "/") for h in r["hits"][:3]]
        assert any(any(e in t for e in expected) for t in top3), \
            f"未召回 {q}: top3={top3} 期望 {expected}"
    search_core.shutdown()
