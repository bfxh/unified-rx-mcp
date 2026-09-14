# -*- coding: utf-8 -*-
"""S129：ide_risk_rank 风险榜契约（高扇入×无测试排序 + 历史趋势 + 如实边界）。"""
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import registry  # noqa: E402
import tools  # noqa: E402,F401
from tools import ide_riskrank as rr  # noqa: E402
from tools import scan as scan_mod  # noqa: E402

pytestmark = pytest.mark.skipif(scan_mod._rx_scan_exe() is None,
                                reason="rx-scan.exe 缺失")


def _mkrepo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "core.py").write_text("def hot():\n    return 1\n", encoding="utf-8")
    for i in range(5):
        (tmp_path / f"c{i}.py").write_text(
            f"from core import hot\n\n\ndef use_{i}():\n    return hot()\n",
            encoding="utf-8")
    (tmp_path / "orphan.py").write_text("def cold():\n    return 1\n", encoding="utf-8")
    (tmp_path / "solo.py").write_text("def warm():\n    return 2\n", encoding="utf-8")
    (tmp_path / "z.py").write_text(
        "from solo import warm\n\n\ndef z_use():\n    return warm()\n", encoding="utf-8")
    (tmp_path / "test_solo.py").write_text("def test_warm():\n    assert True\n",
                                           encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "helper.py").write_text(
        "def shared():\n    return 3\n", encoding="utf-8")
    (tmp_path / "m.py").write_text(
        "from tests.helper import shared\n\n\ndef m_use():\n    return shared()\n",
        encoding="utf-8")
    return tmp_path


def _rank(root, **extra):
    r = registry.call("ide_risk_rank", {"root": str(root), "record": False, **extra})
    assert r.get("ok"), r
    return r["result"]


def test_rank_orders_high_fanin_untested_first(tmp_path):
    root = _mkrepo(tmp_path)
    res = _rank(root)
    assert res["mode"] == "rank"
    rows = {r["file"]: r for r in res["rows"]}
    assert "core.py" in rows, res
    top = res["rows"][0]
    assert top["file"] == "core.py" and top["score"] == 10, res["rows"]
    assert top["fan_in"] == 5 and top["has_test"] is False
    assert top["top_def"]["name"] == "hot" and top["top_def"]["fan_in"] == 5
    # 有测试的低扇入文件存在但排后（weight ×1）
    assert rows["solo.py"]["has_test"] is True and rows["solo.py"]["score"] == 1
    assert res["rows"].index(rows["core.py"]) < res["rows"].index(rows["solo.py"])
    # 零扇入文件不入榜（没有"谁在调用它"这个问题）
    assert "orphan.py" not in rows
    assert res["formula"].startswith("score = 扇入")
    assert "静态" in res["note"]


def test_include_tests_switch(tmp_path):
    root = _mkrepo(tmp_path)
    default = {r["file"] for r in _rank(root)["rows"]}
    assert "tests/helper.py" not in default, default   # 测试路径默认排除
    with_tests = {r["file"] for r in _rank(root, include_tests=True)["rows"]}
    assert "tests/helper.py" in with_tests, with_tests


def test_history_record_and_delta(tmp_path):
    root = _mkrepo(tmp_path)
    hdir = tmp_path / "hist"
    r1 = registry.call("ide_risk_rank",
                       {"root": str(root), "record": True, "history_dir": str(hdir)})
    assert r1["ok"] and r1["result"]["recorded"] is True, r1
    registry.call("ide_risk_rank",
                  {"root": str(root), "record": True, "history_dir": str(hdir)})
    h = registry.call("ide_risk_rank",
                      {"root": str(root), "mode": "history", "history_dir": str(hdir)})
    assert h["ok"], h
    res = h["result"]
    assert len(res["runs"]) == 2, res
    assert res["delta"]["total_fanin"] == 0          # 同一仓库两轮：无变化
    assert "window" in res["delta"]
    # JSONL 落盘形状可复核
    with open(hdir / "risk_history.jsonl", encoding="utf-8") as f:
        lines = [json.loads(x) for x in f if x.strip()]
    assert len(lines) == 2 and lines[0]["root"] == str(root)
    assert "top" in lines[0] and "untested_files" in lines[0]


def test_exe_missing_clear_error(tmp_path, monkeypatch):
    root = _mkrepo(tmp_path)
    monkeypatch.setattr(rr, "_rx_scan_exe", lambda: None)
    r = registry.call("ide_risk_rank", {"root": str(root)})
    assert r.get("ok") is False, r
    assert "cargo build" in str(r.get("error")), r


def test_sandbox_and_schema():
    r = registry.call("ide_risk_rank", {"root": r"C:\Windows"})
    assert r.get("ok") is False and "越界" in str(r.get("error")), r
    schema = next(t["inputSchema"] for t in registry.list_tools()
                  if t["name"] == "ide_risk_rank")
    assert "root" in schema.get("required", [])
    assert "history" in schema["properties"]["mode"]["enum"]
