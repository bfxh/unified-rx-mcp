# -*- coding: utf-8 -*-
"""P1 p1_score.score 纯函数回归（P/R 口径锁死）。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "bench"))
sys.path.insert(0, ROOT)

from p1_score import score  # noqa: E402


def test_score_full_matrix():
    rows = [
        {"sample": "bug", "rule_expect": "unwrap", "issues": ["unwrap"]},
        {"sample": "bug", "rule_expect": "unwrap", "issues": []},          # FN
        {"sample": "bug", "rule_expect": "indexing", "issues": ["as_cast"]},  # 家族不符=FN
        {"sample": "clean", "rule_expect": None, "issues": []},            # TN
        {"sample": "clean", "rule_expect": None, "issues": ["bare_except"]},  # FP
    ]
    s = score(rows)
    assert s["tp"] == 1 and s["fn"] == 2 and s["fp"] == 1 and s["tn"] == 1
    assert abs(s["precision"] - 0.5) < 1e-6
    assert abs(s["recall"] - 1 / 3) < 1e-3     # 1/3 = 0.333..，round(,3) 后 0.333
    assert s["per_rule"]["unwrap"] == {"tp": 1, "fn": 1}
    assert s["per_rule"]["indexing"] == {"tp": 0, "fn": 1}


def test_score_empty():
    s = score([])
    assert s["precision"] == 0.0 and s["recall"] == 0.0
