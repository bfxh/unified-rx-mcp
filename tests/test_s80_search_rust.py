# -*- coding: utf-8 -*-
"""S80：code_search Rust 原生化（rx-search.exe）的契约测试。

薄壳转调 + exe 缺失清晰报错 + walk 顺序契约（201 文件判别法）+ engine.py
降级路径的形状兼容。
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


def test_mixed_query_registry_contract(tmp_path):
    _write(tmp_path, "a.py", "fn compute_damage() -> i32 { 42 }\n")
    _write(tmp_path, "b.py", "def calc_amount(): return 1\n")
    r = registry.call("code_search", {"query": "damage 计算", "root": str(tmp_path)})
    assert r["ok"], r
    res = r["result"]
    assert res["total"] >= 1 and res["query"] == "damage 计算"
    h = res["hits"][0]
    assert {"file", "line", "score", "snippet"} <= set(h)
    assert h["line"] >= 1 and isinstance(h["score"], float)


def test_exact_symbol_ranks_line(tmp_path):
    _write(tmp_path, "m.py", "auth gate here\nx2\ny2\nz2\nlet m = AUTH_GATE_SWEEP_MARKER;\n")
    r = registry.call("code_search", {"query": "auth_gate_sweep", "root": str(tmp_path)})
    assert r["ok"] and r["result"]["total"] == 1, r
    assert r["result"]["hits"][0]["line"] == 5


def test_not_a_dir_structured_error(tmp_path):
    r = registry.call("code_search", {"query": "x", "root": str(tmp_path / "nope")})
    assert not r["ok"] and "不是目录" in r["error"]


def test_empty_query_rejected_via_exit2(tmp_path):
    """空查询：exe 用法级拒绝（exit 2）→ ValueError 包络。旧行为是 total=0，
    新契约改为显式拒绝（S80 定契约，见 ROUNDLOG）。"""
    r = registry.call("code_search", {"query": "", "root": str(tmp_path)})
    assert not r["ok"] and "query 必填" in r["error"]


def test_k_caps_results(tmp_path):
    for i in range(5):
        _write(tmp_path, f"f{i}.py", "def widget_part(): pass\n")
    r = registry.call("code_search", {"query": "widget part", "root": str(tmp_path), "k": 2})
    assert r["ok"] and r["result"]["total"] == 2, r


def test_walk_files_before_dirs_under_cap(tmp_path):
    """201 文件判别法：字母序混排 DFS 会让 sub/ 烧光 200 名额、z.py 落榜；
    每层先文件后目录（os.walk 结构）则 z.py 必在语料。"""
    _write(tmp_path, "a.py", "roota alphamarker_base\n")
    _write(tmp_path, "z.py", "zeta_zfind_fn\n")
    for i in range(199):
        _write(tmp_path, f"sub/f{i:03d}.py", "def filler(): pass\n")
    r = registry.call("code_search", {"query": "zeta_zfind_fn", "root": str(tmp_path)})
    assert r["ok"] and r["result"]["total"] == 1, r
    assert r["result"]["hits"][0]["file"].endswith("z.py")


def test_skips_git_dir_and_txt(tmp_path):
    _write(tmp_path, ".git/x.py", "uniquemarker123\n")
    _write(tmp_path, "d.txt", "uniquemarker123\n")
    r = registry.call("code_search", {"query": "uniquemarker123", "root": str(tmp_path)})
    assert r["ok"] and r["result"]["total"] == 0, r


def test_exe_missing_clear_error(tmp_path, monkeypatch):
    """exe 缺失必须清晰报错，不静默降级（S79 既定政策）。"""
    monkeypatch.setenv("UNIFIED_RX_RS_EXE", r"Z:\nope\rx-search.exe")
    monkeypatch.setenv("TEMP", str(tmp_path))  # 惯例路径也指到空处
    r = registry.call("code_search", {"query": "x", "root": str(tmp_path)})
    assert not r["ok"] and "rx-search.exe 不存在" in r["error"]


def test_schema_contract():
    ent = registry._TOOLS["code_search"]
    assert ent["group"] == "search"
    assert ent["schema"]["required"] == ["query"]
    assert set(ent["schema"]["properties"]) == {"query", "root", "k"}


def test_positional_and_default_root(tmp_path, monkeypatch):
    """engine.py 降级路径按位置传 (query, root, limit)，root=None → cwd。"""
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "a.py", "defaultroot_marker fn\n")
    r = tsearch.code_search("defaultroot_marker", None, 10)
    assert r["total"] == 1, r
