#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""test_cost_core.py — 成本核算测试（token 估算/单价/汇总）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import cost_core as cc  # noqa: E402


def test_estimate_tokens_mixed():
    # 4 中文（≈4 token）+ 16 ASCII（≈4 token）
    assert cc.estimate_tokens("你好 world hello 12345") == 6


def test_estimate_tokens_empty():
    assert cc.estimate_tokens("") == 0
    assert cc.estimate_tokens(None if False else "") == 0


def test_estimate_tokens_ascii():
    # "hello world" 11 字符 → 11//4 + 1 = 3（max(1, 2) = 2？11//4=2 → max(1,2)=2 + 0 = 2）
    assert cc.estimate_tokens("hello world") == 2


def test_estimate_cost_deepseek():
    c = cc.estimate_cost(1_000_000, 500_000, "deepseek-chat")
    assert c["cost_usd"] == round(0.27 + 0.55, 6)  # 0.82
    assert c["tokens_total"] == 1_500_000
    assert c["cost_cny"] == round(0.82 * 7.2, 4)


def test_estimate_cost_unknown_model_falls_back():
    c = cc.estimate_cost(1000, 0, "no-such-model")
    assert c["model"] == "no-such-model"
    assert c["price_in_per_1m"] == cc.MODEL_PRICES["default"][0]


def test_code_cost():
    c = cc.code_cost("def f():\n    return 1\n")
    assert c["tokens_total"] > 0
    assert c["cost_usd"] >= 0


def test_summarize_buckets():
    import time
    now = time.time()
    records = [
        {"ts": now, "tool": "bug_scan", "task": "projA", "tokens_in": 100,
         "tokens_out": 200, "duration_ms": 10},
        {"ts": now, "tool": "bug_scan", "task": "projA", "tokens_in": 50,
         "tokens_out": 50, "duration_ms": 5},
        {"ts": now, "tool": "math_ops", "task": "projB", "tokens_in": 10,
         "tokens_out": 5, "duration_ms": 1},
    ]
    s = cc.summarize(records, model="deepseek-chat")
    assert s["totals"]["calls"] == 3
    assert s["totals"]["tokens_in"] == 160
    assert s["totals"]["tokens_out"] == 255
    assert s["totals"]["cost_usd"] > 0
    by_tool = {b["key"]: b for b in s["by_tool"]}
    assert by_tool["bug_scan"]["calls"] == 2
    assert by_tool["bug_scan"]["tokens_in"] == 150
    by_proj = {b["key"]: b for b in s["by_project"]}
    assert by_proj["projA"]["calls"] == 2
    by_day = s["by_day"]
    assert len(by_day) == 1  # 同一天


def test_summarize_empty():
    s = cc.summarize([], model="deepseek-chat")
    assert s["ok"] is True
    assert s["totals"]["calls"] == 0
    assert s["totals"]["cost_usd"] == 0.0


# ── server 集成：cost_report 工具 ──────────────────────────────────────

def test_cost_report_estimate_via_server():
    import server
    r = server._call("cost_report", {"action": "estimate", "text": "你好 world"})
    assert r[0].text
    import json as _json
    d = _json.loads(r[0].text)
    assert d["ok"] is True
    assert d["tokens_total"] > 0
    assert "cost_usd" in d


def test_cost_report_summary_via_server():
    import server
    r = server._call("cost_report", {"action": "summary", "model": "deepseek-chat"})
    import json as _json
    d = _json.loads(r[0].text)
    assert d["ok"] is True
    assert "totals" in d and "by_tool" in d
    assert d["totals"]["calls"] >= 0


def test_cost_report_code_via_server():
    import server
    r = server._call("cost_report", {"action": "code",
                                     "path": os.path.join(os.path.dirname(__file__),
                                                          "cost_core.py")})
    import json as _json
    d = _json.loads(r[0].text)
    assert d["ok"] is True
    assert d["files"] >= 1
    assert d["tokens_total"] > 0


def test_cost_report_bad_action():
    import server
    r = server._call("cost_report", {"action": "nope"})
    assert "Error" in r[0].text
