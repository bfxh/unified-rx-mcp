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


# ---------- S163：统计日志轮转（此前无上限：实测本机 40.8MB 且每次调用追加 ~60B）----------

def _redirect_stats(monkeypatch, tmp_path):
    """把打点落点重定向到 tmp（与套件既有纪律一致：不污染真实统计）。"""
    d = tmp_path / ".unified-rx"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(registry, "_stats_path", lambda: str(d / "stats.jsonl"))
    return d


def test_stats_rotation_keeps_history_readable(monkeypatch, tmp_path):
    """S163：超阈值 ⇒ 转分片（带时间戳）；**读方跨分片** ⇒ 轮转不丢历史。

    关键假设（值得单测）：轮转若只改文件名而读方只看当前文件，等于**静默丢历史**——
    这正是"日志轮转"最常见的坑。
    """
    d = _redirect_stats(monkeypatch, tmp_path)
    monkeypatch.setenv("UNIFIED_RX_STATS_MAX_MB", "0.001")   # 1KB（支持小数，否则静默回落 8MB）
    monkeypatch.setenv("UNIFIED_RX_STATS_KEEP", "2")
    for i in range(600):
        registry._record_stats("bulk", 1)
    import re as _re
    shards = [f for f in os.listdir(d) if _re.match(r'^stats\.\d{8}-\d{6}\.jsonl$', f)]
    assert shards, "超阈值后应产生分片"
    assert len(shards) <= 2, f"KEEP=2 却留了 {len(shards)} 个分片"

    from tools import ops
    monkeypatch.setattr(ops, "_STATS_FILE", str(d / "stats.jsonl"))
    r = ops.usage_stats(top=5)
    assert r.get("total_calls", 0) > 0, "跨分片读失败：轮转把历史弄丢了"


def test_stats_rotation_off_by_zero(monkeypatch, tmp_path):
    """阈值 0 = 关闭轮转（宿主可显式关掉）。"""
    d = _redirect_stats(monkeypatch, tmp_path)
    monkeypatch.setenv("UNIFIED_RX_STATS_MAX_MB", "0")
    for i in range(400):
        registry._record_stats("bulk", 1)
    import re as _re2
    assert not [f for f in os.listdir(d) if _re2.match(r"^stats\.\d{8}-\d{6}\.jsonl$", f)], \
        "阈值 0 时不该轮转"


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
