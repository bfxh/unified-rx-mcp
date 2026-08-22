# -*- coding: utf-8 -*-
"""ciopt_batch / dep_graph / user_sim 新工具契约测试（2026-08-23 第二批）。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402


def _call_text(name, args):
    r = server._call(name, args)
    return "".join(getattr(t, "text", str(t)) for t in r)


def _parse(name, args):
    return json.loads(_call_text(name, args))


# ── ciopt_batch ─────────────────────────────────────────────────────────
def test_ciopt_batch_value_batch():
    d = _parse("ciopt_batch", {"func": "string_case_to_uppercase",
                               "input": ["hello", "world"]})
    assert d["ok"] is True, d
    assert d["mode"] == "value-batch", d
    assert d["results"] == ["HELLO", "WORLD"], d


def test_ciopt_batch_object_batch():
    d = _parse("ciopt_batch", {"func": "sorting_algorithms_quick_sort",
                               "input": [{"arr": [3, 1, 2]}, {"arr": [9, 8]}]})
    assert d["ok"] is True, d
    assert d["mode"] == "object-batch", d
    assert d["results"] == ["[1, 2, 3]", "[8, 9]"], d


def test_ciopt_batch_unknown_func():
    # _call 对工具抛 ValueError 包装为 "Error: ..." 文本（server 契约）
    text = _call_text("ciopt_batch", {"func": "not_exists_func"})
    assert text.startswith("Error"), text
    assert "未知纯函数" in text, text


def test_ciopt_batch_file_io(tmp_path):
    inp = tmp_path / "in.json"
    out = tmp_path / "out.json"
    inp.write_text('[{"arr": [5, 4, 3]}]', encoding="utf-8")
    d = _parse("ciopt_batch", {"func": "sorting_algorithms_quick_sort",
                               "input_file": str(inp),
                               "output_file": str(out)})
    assert d["ok"] is True, d
    assert d["results"] == ["[3, 4, 5]"], d
    assert out.exists(), "结果应落盘"
    assert json.loads(out.read_text(encoding="utf-8"))["results"] == ["[3, 4, 5]"]


# ── dep_graph ───────────────────────────────────────────────────────────
def test_dep_graph_small(tmp_path):
    (tmp_path / "a.py").write_text("import b\nimport os\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("x = 1\n", encoding="utf-8")
    d = _parse("dep_graph", {"path": str(tmp_path), "full": True})
    assert d["ok"] is True, d
    assert d["edge_count"] == 1, d  # a.py -> b.py（os 是标准库被过滤）
    assert "a.py" in d.get("edges", {}), d


def test_dep_graph_missing_dir(tmp_path):
    d = _parse("dep_graph", {"path": str(tmp_path / "nope")})
    assert d["ok"] is False, d


# ── user_sim（无副作用子集：wait 纯等待，不碰鼠标键盘）──────────────────
def test_user_sim_wait_only():
    d = _parse("user_sim", {"actions": [{"action": "wait", "ms": 20}]})
    assert d["ok"] is True, d
    assert d["steps"] == 1, d


def test_user_sim_unknown_action():
    d = _parse("user_sim", {"actions": [{"action": "fly"}]})
    assert d["ok"] is False, d
    assert any("未知操作" in e.get("error", "") for e in d.get("errors", [])), d


def test_user_sim_too_many_steps():
    d = _parse("user_sim", {"actions": [{"action": "wait", "ms": 1}] * 101})
    assert d["ok"] is False, d
