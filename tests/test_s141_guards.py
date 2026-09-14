# -*- coding: utf-8 -*-
"""S141 契约：烧量护栏——QPM/日量默认值标定、日计数跨重启持久化、
burnwatch 会话哨兵（分级告警 + 开关）、session_burn 工具、打点热路径挂钩。"""
import time

import registry
from tools import breaker, burnwatch, ops


def test_qpm_and_daily_defaults_calibrated(monkeypatch):
    """9/14 实测标定：正常重度日峰值 ~1600 次/分钟 → 600 会误伤；3000 留余量仍拦万级洪峰。"""
    monkeypatch.delenv("UNIFIED_RX_GLOBAL_QPM", raising=False)
    monkeypatch.delenv("UNIFIED_RX_DAILY_ALERT", raising=False)
    assert breaker._global_qpm() == 3000
    assert breaker._daily_alert_at() == 100000


def test_daily_count_persists_across_restart(monkeypatch, tmp_path):
    """日计数落盘 + 重读恢复 = server 重启后日账不丢（一日多启场景）。"""
    p = tmp_path / "daily_state.jsonl"
    monkeypatch.setattr(breaker, "_daily_path", lambda: str(p))
    breaker._DAILY.update({"day": None, "count": 0, "alerted": False})
    now = time.time()
    breaker._daily_bump(now)
    assert p.exists()
    restored = breaker._daily_load()          # 等价"重启后模块加载"
    assert restored["count"] == 1, restored
    for _ in range(99):
        breaker._daily_bump(now)
    assert breaker._daily_load()["count"] == 100, "节流落盘点（每 100 次）应写盘"


def test_daily_load_ignores_stale_day(monkeypatch, tmp_path):
    """隔日旧档归零；坏档当空账（绝不抛）。"""
    p = tmp_path / "daily_state.jsonl"
    monkeypatch.setattr(breaker, "_daily_path", lambda: str(p))
    p.write_text('{"day": "2001-01-01", "count": 999, "alerted": true}\n', encoding="utf-8")
    assert breaker._daily_load()["count"] == 0
    p.write_text("not-json\n", encoding="utf-8")
    assert breaker._daily_load() == {"day": None, "count": 0, "alerted": False}


def test_burnwatch_alarms_once_per_tier(monkeypatch):
    """体积越档告警：同档不重复，升档再报（16MB→1档，31MB→2档，阈值 15MB）。"""
    alarms = []
    monkeypatch.setattr(burnwatch, "_alarm",
                        lambda rule, msg, level="WARN": alarms.append((rule, msg)))
    monkeypatch.setenv("UNIFIED_RX_BURN_MB", "15")
    monkeypatch.setattr(burnwatch, "_LAST", 0.0)
    monkeypatch.setattr(burnwatch, "_SEEN", {})
    mt = int(time.time())
    sess = [{"file": "model-io-sess_abcdef12-0000.jsonl", "mb": 16.0, "mtime": mt}]
    monkeypatch.setattr(burnwatch, "sessions", lambda: sess)
    burnwatch.maybe_check()
    assert len(alarms) == 1 and alarms[0][0] == "session_burn"
    assert "abcdef12" in alarms[0][1] and "16.0MB" in alarms[0][1]
    monkeypatch.setattr(burnwatch, "_LAST", 0.0)
    burnwatch.maybe_check()
    assert len(alarms) == 1, "同会话同档不得重复告警"
    sess[0] = {"file": "model-io-sess_abcdef12-0000.jsonl", "mb": 31.0, "mtime": mt}
    monkeypatch.setattr(burnwatch, "_LAST", 0.0)
    burnwatch.maybe_check()
    assert len(alarms) == 2, "升档应再告警一次"


def test_burnwatch_skips_stale_sessions(monkeypatch):
    """陈旧大文件不告警（已结束的会话不再刷旧账，重启后不炸三条）。"""
    alarms = []
    monkeypatch.setattr(burnwatch, "_alarm",
                        lambda rule, msg, level="WARN": alarms.append(rule))
    monkeypatch.setenv("UNIFIED_RX_BURN_MB", "15")
    monkeypatch.setattr(burnwatch, "_LAST", 0.0)
    monkeypatch.setattr(burnwatch, "_SEEN", {})
    monkeypatch.setattr(burnwatch, "sessions", lambda: [
        {"file": "model-io-sess_old00000-1.jsonl", "mb": 99.0,
         "mtime": int(time.time()) - 7200}])
    burnwatch.maybe_check()
    assert alarms == []


def test_burnwatch_off_switch(monkeypatch):
    """UNIFIED_RX_BURN_MB=0 → 整体关闭，不探测不告警。"""
    alarms = []
    monkeypatch.setattr(burnwatch, "_alarm",
                        lambda rule, msg, level="WARN": alarms.append(rule))
    monkeypatch.setenv("UNIFIED_RX_BURN_MB", "0")
    monkeypatch.setattr(burnwatch, "_LAST", 0.0)
    monkeypatch.setattr(burnwatch, "sessions",
                        lambda: [{"file": "model-io-sess_zzz.jsonl", "mb": 999.0, "mtime": 0}])
    burnwatch.maybe_check()
    assert alarms == []


def test_session_burn_tool_reports_sessions(monkeypatch):
    monkeypatch.setenv("UNIFIED_RX_BURN_MB", "15")
    monkeypatch.setattr(burnwatch, "sessions",
                        lambda: [{"file": "model-io-sess_ffeeddcc-1.jsonl", "mb": 24.0,
                                  "mtime": 0}])
    out = ops.session_burn()
    assert out["threshold_mb"] == 15
    s = out["sessions"][0]
    assert s["est_tokens"] == int(24.0 * 280000)
    assert "28万" in out["note"]


def test_record_stats_pings_burnwatch(monkeypatch):
    """打点热路径必须挂上哨兵（节流/异常兜底在哨兵内部）。"""
    calls = []
    monkeypatch.setattr(burnwatch, "maybe_check", lambda: calls.append(1))
    registry.clear_request_context()
    registry.call("breaker_status", {})
    assert calls, "registry._record_stats 未调用 burnwatch.maybe_check"
