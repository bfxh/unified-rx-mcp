# -*- coding: utf-8 -*-
"""telemetry_status / telemetry_query 工具测试（阶段1：AI 可读遥测快照）。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import telemetry_core  # noqa: E402
import server  # noqa: E402


def _seed(monkeypatch, tmp_path):
    d = str(tmp_path / "state")
    os.makedirs(d, exist_ok=True)
    monkeypatch.setenv("UNIFIED_RX_STATE_DIR", d)
    monkeypatch.setenv("RX_TELEMETRY", "1")
    telemetry_core._ENABLED = None
    telemetry_core.shutdown()
    server._call("math_ops", {"action": "div", "a": 10, "b": 2})
    server._call("no_such_tool_xyz", {})
    telemetry_core.tick_hb("daemon-self", 123456.0)
    telemetry_core.flush()
    return d


def test_telemetry_status_snapshot(monkeypatch, tmp_path):
    """快照：聚合 + 慢工具 TOP + 心跳表，且自身不产生遥测（防递归）。"""
    _seed(monkeypatch, tmp_path)
    r = server._call("telemetry_status", {})
    d = json.loads(r[0].text)
    assert d["ok"] is True
    assert d["summary"]["total_calls"] == 2
    assert d["summary"]["total_err"] == 1
    tools = {t["tool"] for t in d["slowest_tools"]}
    assert "math_ops" in tools and "no_such_tool_xyz" in tools
    assert "daemon-self" in d["heartbeats"]
    # 防递归：status 调用未产生新 tool 记录
    r2 = server._call("telemetry_status", {})
    d2 = json.loads(r2[0].text)
    assert d2["summary"]["total_calls"] == 2


def test_telemetry_query_filters(monkeypatch, tmp_path):
    """查询：limit + error 状态过滤。"""
    _seed(monkeypatch, tmp_path)
    r = server._call("telemetry_query", {"limit": 10})
    q = json.loads(r[0].text)
    assert q["ok"] is True
    kinds = {x.get("kind") for x in q["records"]}
    assert kinds == {"tool", "hb"}
    r2 = server._call("telemetry_query", {"status": "error"})
    q2 = json.loads(r2[0].text)
    assert q2["count"] == 1
    assert q2["records"][0]["tool"] == "no_such_tool_xyz"


def test_telemetry_tools_registered():
    """两个工具在注册表（AI 可见可调用）。"""
    assert "telemetry_status" in server._TOOLS
    assert "telemetry_query" in server._TOOLS
    assert "telemetry_snapshot" in server._TOOLS


def test_alarm_check_rules(monkeypatch, tmp_path):
    """告警规则：tool_err_rate 触发 + 30 分钟去重 + alarms.jsonl 落盘。"""
    d = str(tmp_path / "state")
    os.makedirs(d, exist_ok=True)
    monkeypatch.setenv("UNIFIED_RX_STATE_DIR", d)
    monkeypatch.setenv("RX_TELEMETRY", "1")
    telemetry_core._ENABLED = None
    telemetry_core.shutdown()
    # 4 次调用 3 次 error（错误率 75% > 50% 阈值）
    for i in range(4):
        telemetry_core.tick_tool("flaky_tool", None, 2.0, i >= 3, "boom")
    telemetry_core.flush()
    r1 = json.loads(server._call("alarm_check", {})[0].text)
    assert r1["ok"] is True
    rules = [(a["rule"], a["target"]) for a in r1["new"]]
    assert ("tool_err_rate", "flaky_tool") in rules
    # 去重：第二轮无新告警
    r2 = json.loads(server._call("alarm_check", {})[0].text)
    assert len(r2["new"]) == 0
    # alarms.jsonl 落盘
    path = os.path.join(d, "alarms.jsonl")
    assert os.path.exists(path)
    alarms = telemetry_core.read_alarms(10)
    assert any(a["rule"] == "tool_err_rate" for a in alarms)


def test_alarm_check_stale_loop(monkeypatch, tmp_path):
    """卡死循环 → CRITICAL 告警。"""
    d = str(tmp_path / "state")
    os.makedirs(d, exist_ok=True)
    monkeypatch.setenv("UNIFIED_RX_STATE_DIR", d)
    monkeypatch.setenv("RX_TELEMETRY", "1")
    telemetry_core._ENABLED = None
    telemetry_core.shutdown()
    import time as _t
    telemetry_core._send({"cmd": "record", "rec": {
        "kind": "hb", "ts": _t.time() - 3600,
        "loop": "daemon-repo", "cycle_ms": 100.0}})
    telemetry_core.flush()
    r = json.loads(server._call("alarm_check", {})[0].text)
    new = [(a["rule"], a["target"], a["level"]) for a in r["new"]]
    assert ("daemon_stale", "daemon-repo", "CRITICAL") in new


def test_telemetry_snapshot_health(monkeypatch, tmp_path):
    """一键体检包：卡死检测 + 最近错误 + verdict。"""
    d = str(tmp_path / "state")
    os.makedirs(d, exist_ok=True)
    monkeypatch.setenv("UNIFIED_RX_STATE_DIR", d)
    monkeypatch.setenv("RX_TELEMETRY", "1")
    telemetry_core._ENABLED = None
    telemetry_core.shutdown()
    import time as _t
    # 正常心跳 + 一小时前的"卡死"心跳 + 错误调用
    telemetry_core.tick_hb("daemon-self", 300000.0)
    telemetry_core._send({"cmd": "record", "rec": {
        "kind": "hb", "ts": _t.time() - 3600,
        "loop": "daemon-repo", "cycle_ms": 100.0}})
    server._call("no_such_tool_abc", {})
    telemetry_core.flush()
    r = server._call("telemetry_snapshot", {})
    d2 = json.loads(r[0].text)
    assert d2["ok"] is True
    loops = d2["health"]["loops"]
    assert loops["daemon-self"]["stale"] is False
    assert loops["daemon-repo"]["stale"] is True
    assert "daemon-repo" in d2["verdict"]
    assert d2["recent_errors"][0]["tool"] == "no_such_tool_abc"
