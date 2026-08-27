# -*- coding: utf-8 -*-
"""l2_score —— L2 基准真上场：规则级 P/R 门禁计算。

标签来源（全部为人工定性，标注见 jsonl）：
- bench/labeled_bugs.jsonl        VoxelForge 真实 fix 提交库（VF-*）
- bench/ya_audit_labels.jsonl     Yan Agent 审计沉淀库（YA-*，2026-08-27 本轮实证）

门禁规则（UPGRADE-E）：
- 对「rule_expect 命中」的条目计 TP；「文本不可判」排除出 P/R 统计并显式列出（防虚高）
- FP 样本来自审计中人工确认的误报集
- 通过线：precision ≥ 0.7 且 FP 样本未覆盖的规则须带 coverage 警示
"""
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_LABEL_FILES = [_HERE / "labeled_bugs.jsonl", _HERE / "ya_audit_labels.jsonl"]

# 人工确认的误报样本（file 片段 → 规则）；来源见各条 audit note
_MANUAL_FP = [
    {"rule": "eval_exec", "sample": "yan-agent-src/lib/dsml-tool-call.js",
     "count": 10, "evidence": "2026-08-27 全部 10 处均为 RegExp.prototype.exec 成员调用"},
]


def load_labels():
    rows = []
    for p in _LABEL_FILES:
        if not p.exists():
            continue
        with open(p, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    rows.append(json.loads(ln))
    return rows


def score():
    rows = load_labels()
    per_rule = defaultdict(lambda: {"tp": 0, "fn_unclear": 0, "excluded": 0})
    excluded_reasons = []
    for r in rows:
        rule = r.get("rule_expect")
        if not rule:
            per_rule["_unmapped"].setdefault("_", {"excluded": 0})["excluded"] += 1
            excluded_reasons.append((r.get("id"), r.get("note", "")[:60]))
            continue
        stat = per_rule[rule]
        if r.get("verdict") == "tp":
            stat["tp"] += 1
        else:
            # 有 rule_expect 但本轮无直接命中证据 → 计入待验证（不冒充 TP，也不冒充排除）
            stat["fn_unclear"] += 1
    table = []
    for fp in _MANUAL_FP:
        table.append({"rule": fp["rule"], "fp_confirmed": fp["count"],
                      "fp_evidence": fp["evidence"],
                      "status": "FAIL(pre-fix)" if fp["count"] > 0 else "-"})

    out = {"labels_total": len(rows), "by_rule": {}, "manual_fp": table}
    for rule, s in sorted(per_rule.items()):
        if rule == "_unmapped":
            continue
        n_judgeable = s["tp"] + s["fn_unclear"]
        out["by_rule"][rule] = {
            **s,
            "precision": round(s["tp"] / n_judgeable, 3) if n_judgeable else None,
            "n_judgeable": n_judgeable,
            # 样本 <3 不允许亮绿灯：宁可标注样本弱也不装确定性
            "gate": ("FAIL" if any(m["rule"] == rule and m["fp_confirmed"] > 0 for m in table)
                     else "PASS" if n_judgeable >= 3 and s["tp"] / n_judgeable >= 0.7
                     else f"WEAK(n={n_judgeable})"),
        }
    out["excluded_items"] = len(excluded_reasons)
    return out


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(json.dumps(score(), ensure_ascii=False, indent=2))
