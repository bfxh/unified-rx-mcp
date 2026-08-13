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


def test_most_active_project_degrade(monkeypatch, tmp_path):
    """_most_active_project 损坏 stats.json 降级（2026-08-14）：坏 JSON → None 不崩溃。"""
    import daemon
    stats_dir = tmp_path / ".unified-rx"
    stats_dir.mkdir()
    stats_file = stats_dir / "stats.json"
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    # 损坏 JSON → None
    stats_file.write_text("{ 坏 JSON !!!", encoding="utf-8")
    r_bad = daemon._most_active_project()
    # 降级语义：不崩溃；返回 None 或缺省候选（本机 VoxelForge-Nexus 存在时）
    assert r_bad is None or r_bad == r"D:\开发\VoxelForge-Nexus", (
        f"损坏 stats.json 应降级（None 或缺省候选）: {r_bad}")
    # 非 dict/非 list → 同上降级
    stats_file.write_text('"just a string"', encoding="utf-8")
    r_str = daemon._most_active_project()
    assert r_str is None or r_str == r"D:\开发\VoxelForge-Nexus"
    # 正常记录：root A 3 次 → A；<3 次 → None
    import json as _j
    stats_file.write_text(_j.dumps({"records": [
        {"root": r"D:\proj\A"}, {"root": r"D:\proj\A"}, {"root": r"D:\proj\A"},
        {"root": r"D:\proj\B"},
    ]}), encoding="utf-8")
    assert daemon._most_active_project() == r"D:\proj\A"
    stats_file.write_text(_j.dumps({"records": [
        {"root": r"D:\proj\C"}, {"root": r"D:\proj\C"},
    ]}), encoding="utf-8")
    # <3 次不返回 C——回退缺省候选（VoxelForge-Nexus 本机存在）
    r_lt3 = daemon._most_active_project()
    assert r_lt3 != r"D:\proj\C" and (r_lt3 is None or r_lt3 == r"D:\开发\VoxelForge-Nexus"), (
        f"<3 次不应返回 C: {r_lt3}")
