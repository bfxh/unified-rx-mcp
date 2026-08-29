# -*- coding: utf-8 -*-
"""S50：诊断历史 JSONL + 跨会话 diff 回归。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "bench"))
sys.path.insert(0, ROOT)

from bench.diag_history import append_diag, diff_since  # noqa: E402


def test_append_and_diff(tmp_path, monkeypatch):
    log = str(tmp_path / "diag_history.jsonl")
    monkeypatch.setattr("bench.diag_history.OUT", log) if False else None
    import bench.diag_history as dh
    monkeypatch.setattr(dh, "OUT", log)
    dh.append_diag("x__y-1", "A", "round0",
                   [{"source": "pylsp", "file": "a.py", "line": 7,
                     "severity": "error", "message": "undefined 'z'"}])
    entries = dh.diff_since("", "x__y-1")
    assert len(entries) == 1 and entries[0]["total"] == 1
    assert entries[0]["diags"][0]["message"] == "undefined 'z'"
    # 空诊断不写
    dh.append_diag("x__y-2", "B", "r", [])
    assert len(dh.diff_since("", "x__y-2")) == 0
