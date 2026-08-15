# -*- coding: utf-8 -*-
"""speculate 测试（阶段3：预测→预执行→缓存命中秒回闭环）。"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402
import speculate  # noqa: E402


def test_speculate_predict_and_execute(tmp_path, monkeypatch):
    """预测 → 预执行白名单只读 → 结果入缓存。"""
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    repo = tmp_path / "repo"
    repo.mkdir()
    f = repo / "a.py"
    f.write_text("def helper():\n    import json\n    x = 1\n", encoding="utf-8")
    d = json.loads(server._call("speculate", {
        "current_file": str(f),
        "recent_paths": [str(repo)]})[0].text)
    assert d["ok"] is True, d
    assert d["predicted"], f"应预测下一步: {d}"
    assert d["stats"]["predicted"] >= 1, d
    executed = [r for r in d["results"] if r["status"] == "executed"]
    assert executed, f"应预执行至少一个: {d['results']}"
    assert d["stats"]["executed"] >= 1, d


def test_speculate_cache_hit(tmp_path, monkeypatch):
    """实际调用命中推测缓存 → 秒回（不重复执行）。"""
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    repo = tmp_path / "repo"
    repo.mkdir()
    f = repo / "a.py"
    f.write_text("def helper():\n    import json\n    x = 1\n", encoding="utf-8")
    # 推测执行
    server._call("speculate", {"current_file": str(f),
                               "recent_paths": [str(repo)]})
    # 实际调用 bug_scan（应与推测的 bug_scan 同参命中）
    d = json.loads(server._call("bug_scan", {"path": str(repo)})[0].text)
    assert d["ok"] is True, d
    # consume 命中验证（直接调 consume_speculated）
    hit = speculate.consume_speculated("bug_scan", {"path": str(repo)})
    assert hit is not None, "推测缓存应命中"
    assert '"ok"' in hit, hit


def test_speculate_whitelist_only():
    """白名单外工具绝不预执行（安全边界）。"""
    preds = speculate._predict_next("a.rs", ["fs_write"], ["C:/x"])
    for p in preds:
        assert p["tool"] in speculate.SPECULATE_WHITELIST, p
