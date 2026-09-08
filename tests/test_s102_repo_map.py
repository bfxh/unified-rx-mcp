# -*- coding: utf-8 -*-
"""S102 repo_map 契约：注册面 / 输出形状 / 聚焦偏置 / 沙盒 / exe 缺失。

算法侧（符号图、PageRank、预算裁剪）由 rust/tests/repomap_test.rs 打同一语义；
本文件守 Python 侧包络：沙盒拒绝与 exe 缺失走 ValueError 包络（ok:false），
工具级错误走 result.error。
"""
import os

import pytest

import registry
import tools  # noqa: F401
from tools import ide_read
from tools import search as search_mod


def _fixture(tmp_path):
    (tmp_path / "lib_a.py").write_text("def alpha():\n    pass\n", encoding="utf-8")
    (tmp_path / "lib_b.py").write_text("def beta():\n    pass\n", encoding="utf-8")
    (tmp_path / "user.py").write_text("alpha()\nalpha()\nalpha()\n", encoding="utf-8")


def test_registration_contract():
    ent = registry._TOOLS["repo_map"]
    assert ent["group"] == "search"
    assert ent["schema"]["required"] == ["root"]
    assert set(ent["schema"]["properties"]) == {"root", "focus", "budget_tokens", "max_files"}


def test_shape_and_engine(tmp_path):
    _fixture(tmp_path)
    r = registry.call("repo_map", {"root": str(tmp_path), "budget_tokens": 4000})
    assert r.get("ok"), r
    res = r["result"]
    assert res["engine"] == "pagerank"
    assert res["files_scanned"] == 3 and res["defs_total"] >= 2
    assert res["defs_shown"] >= 2 and res["truncated"] is False
    assert "alpha" in res["map"] and "beta" in res["map"]


def test_focus_biases_ranking(tmp_path):
    _fixture(tmp_path)
    r = registry.call("repo_map", {"root": str(tmp_path), "focus": ["lib_b"],
                                   "budget_tokens": 4000})
    assert r.get("ok"), r
    first = r["result"]["map"].splitlines()[0]
    assert "beta" in first, r["result"]["map"]


def test_budget_truncates(tmp_path):
    (tmp_path / "many.py").write_text(
        "".join(f"def fn_{i:03d}():\n    pass\n" for i in range(200)), encoding="utf-8")
    r = registry.call("repo_map", {"root": str(tmp_path), "budget_tokens": 60})
    assert r.get("ok"), r
    res = r["result"]
    assert res["defs_shown"] < res["defs_total"] and res["truncated"] is True
    assert res["tokens_est"] <= 60 + 16


def test_sandbox_outside_refused():
    r = registry.call("repo_map", {"root": r"C:\Windows"})
    assert r.get("ok") is False, r
    assert "沙盒外" in str(r.get("error")), r


def test_exe_missing_clear_error(tmp_path, monkeypatch):
    _fixture(tmp_path)
    monkeypatch.setattr(ide_read, "_rx_ide_exe", lambda: None)
    r = registry.call("repo_map", {"root": str(tmp_path)})
    assert r.get("ok") is False, r
    assert "rx-ide.exe" in str(r.get("error")), r
