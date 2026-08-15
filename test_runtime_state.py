# -*- coding: utf-8 -*-
"""runtime_state 测试（阶段4：运行状态回喂——file 状态 + BRP 诚实降级）。"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402
import scan_log_core  # noqa: E402


def test_runtime_state_file(tmp_path, monkeypatch):
    """file 来源：状态入 scan-log（tool=runtime_state 可查——双向反馈）。"""
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    logf = tmp_path / "scan-log.jsonl"
    monkeypatch.setenv("UNIFIED_RX_SCAN_LOG", str(logf))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    d = json.loads(server._call("runtime_state", {
        "path": str(repo), "source": "file",
        "state": {"version": "0.1.0", "scene": "main"}})[0].text)
    assert d["ok"] is True and d["source"] == "file", d
    logs = scan_log_core.query_logs(limit=10)
    rs = [l for l in logs if l.get("tool") == "runtime_state"]
    assert rs, f"应有 runtime_state 记录: {logs[:2]}"
    assert "0.1.0" in rs[0]["summary"] or "状态 2 项" in rs[0]["summary"], rs[0]


def test_runtime_state_brp_degrade(tmp_path, monkeypatch):
    """BRP 未运行 → 诚实降级（不崩溃——记录降级 + 提示启动游戏）。"""
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    logf = tmp_path / "scan-log.jsonl"
    monkeypatch.setenv("UNIFIED_RX_SCAN_LOG", str(logf))
    repo = tmp_path / "repo"
    repo.mkdir()
    d = json.loads(server._call("runtime_state", {
        "path": str(repo), "source": "bevy_brp"})[0].text)
    assert d["ok"] is False and d["degraded"] is True, d
    assert "BRP" in d["note"], d
    logs = scan_log_core.query_logs(limit=10)
    rs = [l for l in logs if l.get("tool") == "runtime_state"]
    assert rs and rs[0]["ok"] is False, f"降级应记录: {logs[:2]}"
