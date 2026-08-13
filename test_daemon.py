"""test_daemon.py — 守护进程循环健壮性测试（2026-08-13）。

覆盖 bug 修复：间隔环境变量非法值时守护线程启动即崩溃（_safe_interval 兜底）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from daemon import _safe_interval  # noqa: E402


def test_safe_interval_normal(monkeypatch):
    monkeypatch.setenv("TEST_INTERVAL", "42")
    assert _safe_interval("TEST_INTERVAL", 300) == 42.0


def test_safe_interval_invalid_falls_back(monkeypatch):
    """非法值（旧版 float() 抛 ValueError 崩溃线程）→ 兜底默认。"""
    monkeypatch.setenv("TEST_INTERVAL", "abc")
    assert _safe_interval("TEST_INTERVAL", 300) == 300.0
    monkeypatch.setenv("TEST_INTERVAL", "1d2s")
    assert _safe_interval("TEST_INTERVAL", 120) == 120.0


def test_safe_interval_empty_uses_default(monkeypatch):
    monkeypatch.setenv("TEST_INTERVAL", "   ")
    assert _safe_interval("TEST_INTERVAL", 600) == 600.0


def test_safe_interval_floor_10s(monkeypatch):
    """下限 10s（防 DoS——间隔配置过小不生效）。"""
    monkeypatch.setenv("TEST_INTERVAL", "0.5")
    assert _safe_interval("TEST_INTERVAL", 300) == 10.0
    monkeypatch.setenv("TEST_INTERVAL", "-3")
    assert _safe_interval("TEST_INTERVAL", 300) == 10.0


def test_safe_interval_unset_uses_default(monkeypatch):
    monkeypatch.delenv("TEST_INTERVAL_UNSET", raising=False)
    assert _safe_interval("TEST_INTERVAL_UNSET", 300) == 300.0
