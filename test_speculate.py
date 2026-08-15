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


def test_predict_from_stats_transition(tmp_path, monkeypatch):
    """stats 数据驱动：调用序列转移概率（A→B 高频 → 预测 B）。"""
    import json as _json
    stats_dir = tmp_path / ".unified-rx"
    stats_dir.mkdir()
    stats = stats_dir / "stats.json"
    # 构造序列：bug_scan → std_check 高频（5 次），bug_scan → game_check 低频（1 次）
    seq = []
    for i in range(5):
        seq.append({"ts": i, "tool": "bug_scan"})
        seq.append({"ts": i + 0.5, "tool": "std_check"})
    seq.append({"ts": 99, "tool": "bug_scan"})
    seq.append({"ts": 99.5, "tool": "game_check"})
    stats.write_text(_json.dumps(seq), encoding="utf-8")
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    preds = speculate._predict_from_stats(["bug_scan"])
    assert preds[0] == "std_check", f"转移高频应预测 std_check: {preds}"
    # 全局回退（last 无转移时）
    preds2 = speculate._predict_from_stats(["nosuch_tool"])
    assert preds2, f"应回退全局高频: {preds2}"
