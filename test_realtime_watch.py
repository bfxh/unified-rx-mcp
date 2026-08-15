# -*- coding: utf-8 -*-
"""realtime_watch 测试（阶段1：改动检测 + 增量扫描打点）。"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import realtime_watch as rw  # noqa: E402


def test_watch_detects_change(tmp_path, monkeypatch):
    """改动文件 → watch_once 检测变更 → 增量扫描 + scan-log 打点。"""
    import scan_log_core
    import server
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    logf = tmp_path / "scan-log.jsonl"
    monkeypatch.setenv("UNIFIED_RX_SCAN_LOG", str(logf))
    repo = tmp_path / "repo"
    repo.mkdir()
    f = repo / "a.py"
    f.write_text("def helper():\n    pass\n", encoding="utf-8")
    monkeypatch.setenv("UNIFIED_RX_WATCH_ROOTS", str(repo))
    # 首次：基线（无变更）
    r = rw.watch_once(scan=False)
    assert r["changed_total"] == 1, f"首轮应检测到新文件: {r}"
    # 第二轮：无变更
    r = rw.watch_once(scan=False)
    assert r["changed_total"] == 0, f"无变更不应检出: {r}"
    # 改动文件 → 检出 + 增量扫描打点（一轮完成——检测即消费）
    f.write_text("def helper():\n    import json\n    x = 1\n", encoding="utf-8")
    monkeypatch.setattr(rw, "_watch_roots", lambda: [str(repo)])
    r = rw.watch_once(scan=True)
    assert r["changed_total"] == 1, f"改动应检出并扫描: {r}"
    assert r["scanned"] == 1, r
    logs = scan_log_core.query_logs(limit=20)
    watch_logs = [l for l in logs if l.get("tool") == "watch_bug"]
    assert watch_logs, f"应有 watch_bug 打点: {logs[:2]}"


def test_watch_status(monkeypatch):
    """watch_status 返回监听配置（线程/间隔/根）。"""
    monkeypatch.setenv("UNIFIED_RX_WATCH_INTERVAL", "1.5")
    s = rw.watcher_status()
    assert s["interval_s"] == 1.5, s
    assert "roots" in s and "tracked_files" in s, s
    assert s["running"] is False, "未 start 前不应 running"
    # start/stop 幂等
    rw._WATCHER.start()
    assert rw.watcher_status()["running"] is True
    rw._WATCHER.start()  # 幂等
    rw._WATCHER.stop()
