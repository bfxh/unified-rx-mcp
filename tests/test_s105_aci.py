# -*- coding: utf-8 -*-
"""S105 ACI 输出纪律契约（ADVANCES P1 末项）。

三件：①空结果显式说明；②截断提示（cursor 续读/缩小范围）；③错误可修复化。
口径：只在确有建议时加 `hint`/尾注——有结果时不加（不制造噪音）。
"""
import os
import tempfile

import registry
import tools  # noqa: F401
from tools import aci


def test_empty_result_hint_code_search(tmp_path):
    (tmp_path / "m.py").write_text("def alpha():\n    pass\n", encoding="utf-8")
    r = registry.call("code_search", {"query": "zzz_never_matches_qqq",
                                      "root": str(tmp_path), "k": 3})
    assert r.get("ok"), r
    assert "无匹配" in r["result"].get("hint", ""), r["result"]


def test_no_hint_when_results_present(tmp_path):
    (tmp_path / "m.py").write_text("def alpha_marker():\n    pass\n", encoding="utf-8")
    r = registry.call("code_search", {"query": "alpha_marker", "root": str(tmp_path), "k": 3})
    assert r.get("ok") and r["result"]["total"] >= 1, r
    assert "hint" not in r["result"], r["result"]


def test_bug_scan_empty_hint_mentions_coverage(tmp_path):
    (tmp_path / "clean.py").write_text("x = 1\n", encoding="utf-8")
    r = registry.call("bug_scan", {"path": str(tmp_path), "max_files": 5,
                                   "__no_cache": True})
    assert r.get("ok"), r
    hint = r["result"].get("hint", "")
    assert "附录 B" in hint and "无漏洞" in hint, hint


def test_truncation_hint_top_level_list(tmp_path):
    for i in range(230):
        (tmp_path / f"f{i:03d}.txt").write_text("", encoding="utf-8")
    r = registry.call("fs_list", {"path": str(tmp_path), "depth": 0, "__no_cache": True})
    res = r["result"]
    assert res["truncated"] is True and res["next_cursor"] == 200, res
    assert "cursor" in res.get("hint", ""), res


def test_truncation_hint_nested_list():
    big = {"items": list(range(300))}
    out = registry._clamp({"wrapper": big}, {})
    assert out["wrapper"]["items_truncated"] is True, out
    assert "嵌套" in out.get("hint", ""), out


def test_error_hint_sandbox_refusal():
    r = registry.call("fs_read", {"path": r"C:\Windows\win.ini"})
    assert r.get("ok") is False, r
    assert "（建议：" in r["error"] and "UNIFIED_RX_SANDBOX" in r["error"], r["error"]


def test_error_hint_schema():
    r = registry.call("fs_read", {})
    assert r.get("ok") is False, r
    assert "（建议：" in r["error"] and "schema" in r["error"], r["error"]


def test_hint_error_idempotent():
    msg = "路径越界（沙盒外）: X"
    once = aci.hint_error(msg)
    twice = aci.hint_error(once)
    assert once == twice and once.count("建议：") == 1, (once, twice)


def test_hint_error_unknown_class_unchanged():
    msg = "某个没见过的错误"
    assert aci.hint_error(msg) == msg
