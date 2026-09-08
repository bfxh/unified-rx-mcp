# -*- coding: utf-8 -*-
"""S108 dep_graph(resolved=true) 契约：语法级解析边接入 + 旧形状零破坏 + 显式降级。

解析语义由 rust/tests/nameres_test.rs 锁定（相对导入/别名/子模块/外部/未找到）；
本文件守 Python 侧：注册面、旧形状不变、exe 缺失如实入 resolved.error。
"""
import json
import os

import pytest

import registry
import tools  # noqa: F401
from tools import scan as scan_mod


def _mkpkg(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "mod_a.py").write_text("def alpha():\n    return 1\n",
                                               encoding="utf-8")
    (tmp_path / "pkg" / "mod_b.py").write_text(
        "from .mod_a import alpha\n", encoding="utf-8")
    return tmp_path


def test_schema_contract():
    ent = registry._TOOLS["dep_graph"]
    assert ent["group"] == "scan"
    assert set(ent["schema"]["properties"]) == {"path", "resolved"}
    assert ent["schema"]["required"] == ["path"]


def test_default_shape_unchanged(tmp_path):
    _mkpkg(tmp_path)
    r = registry.call("dep_graph", {"path": str(tmp_path)})
    assert r.get("ok"), r
    assert "resolved" not in r["result"], r["result"]


@pytest.mark.skipif(scan_mod._rx_scan_exe() is None, reason="rx-scan.exe 缺失")
def test_resolved_edges(tmp_path):
    _mkpkg(tmp_path)
    r = registry.call("dep_graph", {"path": str(tmp_path), "resolved": True})
    assert r.get("ok"), r
    res = r["result"]
    rv = res.get("resolved") or {}
    assert "error" not in rv, rv
    assert rv["stats"]["internal"] == 1, rv
    edge = [e for e in rv["imports"]
            if e["file"] == "pkg/mod_b.py" and e["name"] == "alpha"]
    assert edge and edge[0]["to_file"] == "pkg/mod_a.py" and edge[0]["to_line"] == 1, rv


def test_exe_missing_reports_explicitly(tmp_path, monkeypatch):
    _mkpkg(tmp_path)
    monkeypatch.setattr(scan_mod, "_rx_scan_exe", lambda: None)
    r = registry.call("dep_graph", {"path": str(tmp_path), "resolved": True})
    assert r.get("ok"), r
    res = r["result"]
    assert res["total_files"] >= 1, "基础图不受影响"
    assert "rx-scan.exe" in (res.get("resolved") or {}).get("error", ""), res


def test_sandbox_outside_refused():
    r = registry.call("dep_graph", {"path": r"C:\Windows"})
    assert r.get("ok") is False, r
    assert "沙盒外" in str(r.get("error")), r
