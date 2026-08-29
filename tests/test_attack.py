# -*- coding: utf-8 -*-
"""S55：attack 域测试（此前零引用——test_v2 只是 docstring 提到，从未真调）。

registry 层全往返：病态输入必须被结构化拒绝，绝不崩、绝不产生噪音结果。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import registry  # noqa: E402
import tools  # noqa: E402,F401


def test_input_fuzz_all_structured(tmp_path):
    f = tmp_path / "t.py"
    f.write_text("x = 1\n", encoding="utf-8")
    r = registry.call("input_fuzz", {
        "tool_name": "code_context", "base_args": {"path": str(f)},
        "fuzz_field": "path"})
    assert r["ok"], r.get("error")
    res = r["result"]
    assert res["cases"] == 12 and res["failures"] == 0
    assert all(v["verdict"].startswith("PASS") for v in res["results"])


def test_input_fuzz_unknown_tool(tmp_path):
    r = registry.call("input_fuzz", {"tool_name": "no_such_tool",
                                     "base_args": {}, "fuzz_field": "x"})
    assert not r["ok"] and "未知工具" in r["error"]


def test_big_input_all_pass(tmp_path):
    r = registry.call("big_input", {
        "tool_name": "code_context", "base_args": {"path": "seed"},
        "fuzz_field": "path"})
    assert r["ok"], r.get("error")
    assert r["result"]["all_pass"] is True
    labels = {c["case"] for c in r["result"]["cases"]}
    assert labels == {"str_1mb", "list_100k", "deep_500"}


def test_path_probe_rejects_all_escapes():
    r = registry.call("path_probe", {})
    assert r["ok"], r.get("error")
    res = r["result"]
    assert res["probes"] == 8 and res["all_safe"] is True
