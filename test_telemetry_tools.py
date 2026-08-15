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
