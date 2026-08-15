# -*- coding: utf-8 -*-
"""cov_scan 测试（阶段3：死代码/未用 import 定位）。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cov_scan as cs  # noqa: E402


def _make_proj(tmp_path):
    """测试项目：used_fn 存活 / dead_fn+DeadClass+x 死 / math+os 未用。"""
    p = tmp_path / "proj"
    p.mkdir()
    (p / "alive.py").write_text(
        "import math\nimport os\n\n"
        "def used_fn():\n    return 1\n\n"
        "def dead_fn():\n    return 2\n\n"
        "class DeadClass:\n    pass\n\n"
        "x = used_fn()\n", encoding="utf-8")
    (p / "test_alive.py").write_text(
        "def test_x():\n    assert 1\n", encoding="utf-8")
    return str(p)


def test_dead_code_detection(tmp_path):
    r = cs.cov_scan(_make_proj(tmp_path))
    assert r["ok"] is True
    dead = {d["symbol"] for d in r["dead_code"]}
    assert dead == {"dead_fn", "DeadClass", "x"}
    assert "used_fn" not in dead
    unused = {u["symbol"] for u in r["unused_imports"]}
    assert unused == {"math", "os"}


def test_ignore_test_prefix(tmp_path):
    """test_ 前缀函数与 main 不报死代码。"""
    p = tmp_path / "proj2"
    p.mkdir()
    (p / "a.py").write_text(
        "def test_helper():\n    pass\n\ndef main():\n    pass\n", encoding="utf-8")
    r = cs.cov_scan(str(p))
    dead = {d["symbol"] for d in r["dead_code"]}
    assert "test_helper" not in dead
    assert "main" not in dead


def test_bad_path():
    r = cs.cov_scan("D:/no/such/path/xyz")
    assert r["ok"] is False


def test_cross_file_reference(tmp_path):
    """跨文件引用：B import A 的符号 → A 存活。"""
    p = tmp_path / "proj3"
    p.mkdir()
    (p / "a.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (p / "b.py").write_text("from a import helper\n\nv = helper()\n",
                            encoding="utf-8")
    r = cs.cov_scan(str(p))
    dead = {d["symbol"] for d in r["dead_code"]}
    assert "helper" not in dead
