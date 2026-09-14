# -*- coding: utf-8 -*-
"""S133：attack_cruise 巡航契约（编排既有攻击面工具 + verdict + 失败项不吞）。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import registry  # noqa: E402
import tools  # noqa: E402,F401


def test_registered_and_schema():
    schema = next(t["inputSchema"] for t in registry.list_tools()
                  if t["name"] == "attack_cruise")
    props = schema.get("properties") or {}
    assert {"targets", "battery", "big"} <= set(props), props
    assert registry._TOOLS["attack_cruise"]["group"] == "attack"


def test_cruise_clean_on_self():
    """默认电池跑本包自身：四靶模糊 + 大输入 + gate 审计 + 路径探针全过。"""
    r = registry.call("attack_cruise", {})
    assert r.get("ok"), r
    res = r["result"]
    assert res["verdict"] == "clean", res
    assert res["gates"]["ok"] is True, res["gates"]
    assert res["passive"]["all_safe"] is True, res["passive"]
    assert len(res["fuzz"]) == 4, res["fuzz"]
    assert all(f["failures"] == 0 for f in res["fuzz"]), res["fuzz"]
    assert all(b["all_pass"] for b in res["big"]), res["big"]
    assert res["failures"] == [] and res["errors"] == []


def test_cruise_extra_target_and_unknown_tool_surfaces():
    """增补靶生效；未知工具不吞——进 errors 并 verdict=issues。"""
    good = registry.call("attack_cruise", {
        "battery": False, "big": False,
        "targets": [{"tool_name": "fs_stat",
                     "base_args": {"path": "<pkg>"},
                     "fuzz_field": "path"}]})
    assert good.get("ok"), good
    res = good["result"]
    assert res["verdict"] == "clean", res
    assert [f["tool"] for f in res["fuzz"]] == ["fs_stat"], res["fuzz"]

    bad = registry.call("attack_cruise", {
        "battery": False, "big": False,
        "targets": [{"tool_name": "no_such_tool_zz",
                     "base_args": {}, "fuzz_field": "x"}]})
    assert bad.get("ok"), bad                     # 巡航本体成功
    res2 = bad["result"]
    assert res2["verdict"] == "issues", res2
    assert any("no_such_tool_zz" in e for e in res2["errors"]), res2


def test_cruise_bad_targets_shape_clean_error():
    r = registry.call("attack_cruise", {"targets": [{"nope": 1}]})
    assert r.get("ok") is False, r
    assert "targets 项需为" in str(r.get("error")), r
