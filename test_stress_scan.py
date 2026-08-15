# -*- coding: utf-8 -*-
"""stress_scan 测试（阶段3：并发/容量压力）。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stress_scan as ss  # noqa: E402


def test_stress_log_concurrent(monkeypatch, tmp_path):
    """8 线程并发 append 无丢失（隔离日志文件）。"""
    monkeypatch.setenv("UNIFIED_RX_STATE_DIR", str(tmp_path / "state"))
    r = ss._stress_log(scale=800)
    assert r["ok"] is True, r["detail"]
    assert r["errors"] == 0
    assert r["detail"].endswith("丢 0")


def test_stress_telemetry_concurrent(monkeypatch, tmp_path):
    """8 线程并发 tick 无丢失。"""
    monkeypatch.setenv("UNIFIED_RX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("RX_TELEMETRY", "1")
    import telemetry_core
    telemetry_core._ENABLED = None
    telemetry_core.shutdown()
    r = ss._stress_telemetry(scale=800)
    telemetry_core.shutdown()
    assert r["ok"] is True, r["detail"]
    assert r["errors"] == 0


def test_stress_index(tmp_path):
    """大仓库遍历计时。"""
    p = tmp_path / "repo"
    p.mkdir()
    for i in range(5):
        (p / f"f{i}.py").write_text("x = 1\n", encoding="utf-8")
    r = ss._stress_index(str(p))
    assert r["ok"] is True
    assert r["items"] == 5


def test_stress_file_bad_path():
    r = ss._stress_file("D:/no/such/path/zzz")
    assert r["ok"] is False


def test_stress_scan_auto(monkeypatch, tmp_path):
    """主入口：无 path → log+telemetry 两场景。"""
    monkeypatch.setenv("UNIFIED_RX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("RX_TELEMETRY", "1")
    import telemetry_core
    telemetry_core._ENABLED = None
    telemetry_core.shutdown()
    r = ss.stress_scan(mode="auto", scale=400)
    telemetry_core.shutdown()
    assert r["ok"] is True
    scenes = [s["scene"] for s in r["results"]]
    assert scenes == ["log", "telemetry"]
