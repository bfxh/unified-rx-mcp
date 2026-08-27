# -*- coding: utf-8 -*-
"""tests/test_stats.py —— 统计域测试（T1-T7）。"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import registry
import tools  # noqa: F401


def test_cost_report_has_data():
    """T1: registry 自动打点后 cost_report 有数据。"""
    r = registry.call("cost_report", {})
    assert r["ok"], r
    assert "total_calls" in r["result"]
    # 至少刚才的调用被记录了
    assert r["result"]["total_calls"] >= 1


def test_cost_report_status():
    r = registry.call("cost_report", {"action": "status"})
    assert r["ok"] and r["result"]["exists"] is True


def test_scan_log_trend_compat():
    """T2: 兼容旧版字符串 ts。"""
    r = registry.call("scan_log", {"action": "trend"})
    assert r["ok"], r
    assert isinstance(r["result"]["trend"], list)


def test_backup_requires_root():
    """T3: backup 无 root 正确拒绝（错误语义统一后为顶层 ok:false）。"""
    r = registry.call("backup", {})
    assert not r["ok"], f"缺 root 应拒绝: {r}"
    assert "root" in r.get("error", "")


def test_usage_stats():
    """T4: 使用统计。"""
    r = registry.call("usage_stats", {"top": 3})
    assert r["ok"], r
    assert r["result"]["total_calls"] >= 1
    assert isinstance(r["result"]["freq_top"], list)


def test_trend_analysis():
    """T5: 趋势分析。"""
    r = registry.call("trend_analysis", {"days": 7})
    assert r["ok"], r
    assert r["result"]["trend"] in ("stable", "improving", "worsening")
    assert isinstance(r["result"]["series"], list)


def test_project_health():
    """T6: 健康度评分。"""
    r = registry.call("project_health", {"path": os.path.dirname(os.path.dirname(os.path.abspath(__file__)))})
    assert r["ok"], r
    assert 0 <= r["result"]["score"] <= 100
    assert r["result"]["grade"] in "ABCDF"


def test_lesson_stats():
    """T7: 教训库统计。"""
    r = registry.call("lesson_stats", {"top": 5})
    assert r["ok"], r
    assert r["result"]["total"] >= 0
