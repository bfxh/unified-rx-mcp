# -*- coding: utf-8 -*-
"""telemetry_core 测试（阶段1：工具调用遥测 + daemon 心跳 + 查询）。"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import telemetry_core  # noqa: E402


def _state_dir(monkeypatch, tmp_path):
    """隔离状态目录 + 重置惰性判定。"""
    d = str(tmp_path / "state")
    os.makedirs(d, exist_ok=True)
    monkeypatch.setenv("UNIFIED_RX_STATE_DIR", d)
    monkeypatch.setenv("RX_TELEMETRY", "1")
    telemetry_core._ENABLED = None  # 重置惰性判定
    telemetry_core.shutdown()  # 关掉可能残留的子进程
    return d


def test_tick_tool_and_hb_persist(monkeypatch, tmp_path):
    """工具调用 + 心跳 → telemetry.jsonl 落盘且格式正确。"""
    d = _state_dir(monkeypatch, tmp_path)
    telemetry_core.tick_tool("bug_scan", {"path": "x"}, 12.5, True, "")
    telemetry_core.tick_tool("bug_locate", None, 800.0, False, "too long")
    telemetry_core.tick_hb("daemon-self", 300_000.0)
    telemetry_core.flush()
    path = os.path.join(d, "telemetry.jsonl")
    assert os.path.exists(path), "telemetry.jsonl 应已落盘"
    lines = [l for l in open(path, encoding="utf-8").read().splitlines() if l.strip()]
    assert len(lines) == 3
    r0 = json.loads(lines[0])
    assert r0["kind"] == "tool" and r0["tool"] == "bug_scan"
    assert r0["status"] == "ok" and r0["wall_ms"] == 12.5
    r1 = json.loads(lines[1])
    assert r1["status"] == "error" and r1["err"] == "too long"
    r2 = json.loads(lines[2])
    assert r2["kind"] == "hb" and r2["loop"] == "daemon-self"
    assert r2["cycle_ms"] == 300_000.0


def test_agg_query(monkeypatch, tmp_path):
    """聚合：total/err_rate/工具维度/heartbeats。"""
    d = _state_dir(monkeypatch, tmp_path)
    telemetry_core.tick_tool("math_ops", {"action": "div"}, 2.0, True, "")
    telemetry_core.tick_tool("math_ops", {"action": "div"}, 9.0, False, "boom")
    telemetry_core.tick_hb("daemon-project", 95_000.0)
    telemetry_core.flush()
    agg = telemetry_core.agg()
    assert agg is not None
    assert agg["total_calls"] == 2
    assert agg["total_err"] == 1
    assert agg["overall_err_rate"] == 0.5
    t = agg["tools"]["math_ops"]
    assert t["count"] == 2 and t["err_count"] == 1
    assert t["max_ms"] == 9.0
    assert "daemon-project" in agg["heartbeats"]


def test_tail_last_n(monkeypatch, tmp_path):
    """tail：返回最近 N 条（流式读）。"""
    d = _state_dir(monkeypatch, tmp_path)
    for i in range(5):
        telemetry_core.tick_tool(f"t{i}", None, 1.0, True, "")
    telemetry_core.flush()
    recs = telemetry_core.tail(3) or []
    assert len(recs) == 3
    tools = [r["tool"] for r in recs]
    assert tools == ["t2", "t3", "t4"]


def test_disabled_env(monkeypatch, tmp_path):
    """RX_TELEMETRY=0 → 全部静默（不建子进程、不落盘）。"""
    d = _state_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("RX_TELEMETRY", "0")
    telemetry_core._ENABLED = None
    assert telemetry_core.enabled() is False
    telemetry_core.tick_tool("x", None, 1.0, True, "")
    telemetry_core.tick_hb("daemon-self", 1.0)
    telemetry_core.flush()
    assert not os.path.exists(os.path.join(d, "telemetry.jsonl"))


def test_status_reports_state(monkeypatch, tmp_path):
    """status：路径/已落盘计数。"""
    d = _state_dir(monkeypatch, tmp_path)
    telemetry_core.tick_tool("a", None, 1.0, True, "")
    telemetry_core.flush()
    st = telemetry_core.status()
    assert st is not None
    assert st["flushed"] >= 1
    assert "telemetry.jsonl" in st["path"]
