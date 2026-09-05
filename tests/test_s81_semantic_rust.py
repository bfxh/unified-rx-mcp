# -*- coding: utf-8 -*-
"""S81：code_semantic Rust 原生化（rx-semantic.exe）的契约测试。

薄壳转调 + exe 缺失清晰报错 + 大查询走 stdin（Windows 命令行 32767 码元上限）
+ schema 契约不变 + 旧实现内部函数确已退役。行为回归由旧 test_semantic.py
原样继续承担（薄壳下原样过检即等价证明）。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import registry
import tools  # noqa: F401
import tools.search as tsearch


def _write(root, rel, content):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _tree(tmp_path):
    _write(tmp_path, "clock.rs",
           "pub struct Clock {\n    pub elapsed: f32,\n}\n\nimpl Clock {\n"
           "    // 时钟累加\n    pub fn tick(&mut self, dt: f32) {\n"
           "        self.elapsed += dt;\n    }\n}\n")


def test_search_registry_contract(tmp_path):
    _tree(tmp_path)
    r = registry.call("code_semantic",
                      {"query": "时钟累加", "root": str(tmp_path), "k": 5})
    assert r["ok"], r
    res = r["result"]
    assert res["mode"] == "search" and res["total"] >= 1
    h = res["hits"][0]
    assert {"file", "line", "symbol", "kind", "score", "snippet"} <= set(h)


def test_related_contract(tmp_path):
    _tree(tmp_path)
    r = registry.call("code_semantic",
                      {"query": "tick", "root": str(tmp_path), "mode": "related",
                       "k": 5})
    assert r["ok"], r
    res = r["result"]
    assert res["mode"] == "related" and res["anchor"] == "tick"
    assert all("snippet" not in h for h in res["hits"])


def test_empty_query_legal_for_semantic(tmp_path):
    """code_semantic 空 query 合法（S31 契约，与 code_search 的显式拒绝不同）：
    有语料时 search 返回 total=0 且带 mode 键。"""
    _tree(tmp_path)
    r = registry.call("code_semantic", {"query": "", "root": str(tmp_path)})
    assert r["ok"] and r["result"]["total"] == 0 and "mode" in r["result"], r


def test_mode_rejected_via_exit2(tmp_path):
    """mode 非法：exe 用法级拒绝（exit 2）→ ValueError 包络。"""
    _tree(tmp_path)
    r = registry.call("code_semantic",
                      {"query": "x", "root": str(tmp_path), "mode": "bogus"})
    assert not r["ok"] and "mode 必须是 search 或 related" in r["error"], r


def test_not_a_dir_structured_error(tmp_path):
    r = registry.call("code_semantic", {"query": "x", "root": str(tmp_path / "nope")})
    assert not r["ok"] and "不是目录" in r["error"]


def test_exe_missing_clear_error(tmp_path, monkeypatch):
    """exe 缺失必须清晰报错，不静默降级（S79 既定政策）。"""
    monkeypatch.setenv("UNIFIED_RX_RS_EXE", r"Z:\nope\rx-semantic.exe")
    monkeypatch.setenv("TEMP", str(tmp_path))  # 惯例路径也指到空处
    r = registry.call("code_semantic", {"query": "x", "root": str(tmp_path)})
    assert not r["ok"] and "rx-semantic.exe 不存在" in r["error"]


def test_big_query_rides_stdin(tmp_path):
    """5 万字查询超 Windows 命令行上限（32767 码元）→ 薄壳走 stdin 通道
    （argv 传 "-"，exe 侧等价替换）。旧 test_big_input_smoke 的原样过检依赖此路。"""
    _tree(tmp_path)
    r = registry.call("code_semantic",
                      {"query": "词" * 50000, "root": str(tmp_path), "k": 3})
    assert r["ok"], r
    assert "hits" in r["result"] and "mode" in r["result"]


def test_big_query_search_tool_rides_stdin(tmp_path):
    """code_search 同款大查询通道（S80 的潜在缺口，S81 一并补上）。"""
    _write(tmp_path, "a.py", "def big_query_marker_fn(): pass\n")
    r = registry.call("code_search",
                      {"query": "词" * 40000, "root": str(tmp_path)})
    assert r["ok"], r
    assert "hits" in r["result"]


def test_schema_contract():
    ent = registry._TOOLS["code_semantic"]
    assert ent["group"] == "search"
    assert ent["schema"]["required"] == ["query"]
    assert set(ent["schema"]["properties"]) == {"query", "root", "mode", "k"}
    assert ent["schema"]["properties"]["mode"]["enum"] == ["search", "related"]


def test_positional_and_default_root(tmp_path, monkeypatch):
    """按位置传 (query, root, mode, k)，root=None → cwd。"""
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "a.py", "def positional_marker_fn():\n    pass\n")
    r = tsearch.code_semantic("positional_marker_fn", None, "search", 8)
    assert r["mode"] == "search" and r["total"] == 1, r


def test_old_python_internals_retired():
    """S31 纯 Python 实现已退役：内部函数不得复活（薄壳是唯一路径）。"""
    for name in ("_sem_defs", "_sem_vec", "_cosine", "_SEM_CACHE",
                 "_get_sem_index", "_SEM_DEF_RE", "_tokenize",
                 "_fingerprints", "_INDEX_EXTS", "_MAX_FILES", "_STOPWORDS"):
        assert not hasattr(tsearch, name), name
