#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""scan_trend.py — 扫描日志趋势分析（MCP_OPTIMIZATION_PLAN M6）。

从 scan-log 统计规则/工具命中趋势：
  - 工具调用频率（哪些工具用得多——智能体偏好）
  - 规则命中率（bug 规则高频命中 → 提权关注；扫描频繁但 0 命中 → 降噪候选）
  - 时间趋势（近 7 天 vs 之前——新问题爆发/收敛）

消费 scan_log_core.query_logs 的数据（本地无依赖）。
"""

import json
import os
import time
from collections import Counter


def analyze(logs: list[dict], window_days: int = 7) -> dict:
    """日志列表 → 趋势分析。logs: [{ts, tool, issues, rule...}]。"""
    now = time.time()
    window = window_days * 86400
    recent = []
    for l in logs:
        ts = l.get("ts", 0)
        try:
            ts = float(ts)
        except (TypeError, ValueError):
            continue
        if ts >= now - window:
            recent.append(l)

    # 1. 工具调用频率
    tool_freq = Counter(str(l.get("tool", "?")) for l in recent)
    # 2. 规则命中（issue 条目里的 rule 字段；或日志本身带 rule）
    rules = Counter()
    for l in recent:
        issues = l.get("issues") or l.get("result") or []
        if isinstance(issues, list):
            for it in issues:
                if isinstance(it, dict) and it.get("rule"):
                    rules[str(it["rule"])] += 1
        elif isinstance(issues, str):
            # 文本日志里找 rule 模式
            for seg in str(issues).replace(",", " ").split():
                if seg and seg[0].isalpha() and len(seg) <= 24:
                    rules[seg] += 1
    # 3. 高频问题规则（提权候选）
    hot_rules = rules.most_common(10)
    # 4. 工具用得多但 0 命中的（降噪候选）
    noisy = {t: c for t, c in tool_freq.items() if c >= 3
             and all(r not in str(l) for l in recent[:50])}

    return {
        "ok": True,
        "window_days": window_days,
        "total_logs": len(logs),
        "recent_logs": len(recent),
        "tool_frequency": tool_freq.most_common(10),
        "rule_hits": hot_rules,
        "hot_rules_advice": [f"规则 {r} 命中 {c} 次——建议重点排查/规则提权"
                             for r, c in hot_rules[:3]],
        "noisy_tools": list(noisy.keys()),
        "note": "趋势数据来自 scan-log（日志→统计→增强规则闭环）",
    }
