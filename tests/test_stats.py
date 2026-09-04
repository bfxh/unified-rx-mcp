# -*- coding: utf-8 -*-
"""tests/test_stats.py —— 统计域测试（T1-T7）。"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import registry
import tools  # noqa: F401


def test_usage_stats_supersedes_cost_report():
    """T1+S15: registry 自动打点 → usage_stats 是调用统计唯一出口（cost_report 已并入）。"""
    r = registry.call("usage_stats", {"top": 3})
    assert r["ok"], r
    assert "total_calls" in r["result"]
    assert isinstance(r["result"]["freq_top"], list)
    # 至少刚才的调用被记录了
    assert r["result"]["total_calls"] >= 1


def test_scan_log_trend_and_projection_shape():
    """T2/T5 合并: scan_log trend action 兼容旧字符串 ts（S15 起 trend 唯一出口）。"""
    r = registry.call("scan_log", {"action": "trend"})
    assert r["ok"], r
    assert isinstance(r["result"]["trend"], list)


def test_backup_requires_root():
    """T3: backup 无 root 正确拒绝（错误语义统一后为顶层 ok:false）。"""
    r = registry.call("backup", {"__authorized": True})  # S75 挂门后先过授权再查 root
    assert not r["ok"], f"缺 root 应拒绝: {r}"
    assert "root" in r.get("error", "")


def test_usage_stats():
    """T4: 使用统计。"""
    r = registry.call("usage_stats", {"top": 3})
    assert r["ok"], r
    assert r["result"]["total_calls"] >= 1
    assert isinstance(r["result"]["freq_top"], list)


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
