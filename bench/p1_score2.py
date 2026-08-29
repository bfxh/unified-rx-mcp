# -*- coding: utf-8 -*-
"""p1_score2.py —— 独立人工标注 vs bug_scan：泛化 P/R 测量。

规则：scan issue 与人工标注同行同族 → unsafe=TP / safe=FP；
人工 unsafe 但 scan 未在同线同族命中 → FN；scan 命中但无人工标注行 → late 桶（复核）。
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import registry  # noqa: E402
import tools  # noqa: E402,F401

OUT = os.path.join(HERE, "results", "p1_manual_pr.json")


def scan_text(src, suffix):
    d = tempfile.mkdtemp(prefix="p1m")
    fp = os.path.join(d, "f" + suffix)
    with open(fp, "w", encoding="utf-8", newline="") as f:
        f.write(src)
    r = registry.call("bug_scan", {"path": fp})
    return r.get("result", {}).get("issues") or []


def main():
    snaps = [json.loads(l) for l in
             open(os.path.join(HERE, "p1_manual_labels.jsonl"), encoding="utf-8")
             if l.strip()]
    tp = fn = fp = late = 0
    detail = []
    for s in snaps:
        src = open(os.path.join(HERE, "manual_snaps", s["snap_file"]),
                   encoding="utf-8", errors="replace").read()
        issues = scan_text(src, os.path.splitext(s["file"])[1])
        unsafe = {u["line"]: u for u in s["labels"]["unsafe"]}
        hit_unsafe = set()
        for i in issues:
            ln = i["line"]
            if ln in unsafe and unsafe[ln]["rule"] == i["rule"]:
                tp += 1
                hit_unsafe.add(ln)
                detail.append({**i, "snap": s["snap_id"], "verdict": "TP"})
            elif any(u["line"] == ln for u in s["labels"]["unsafe"]):
                # 同行不同族：scan 命中了别的规则——人工只标了主要风险
                late += 1
                detail.append({**i, "snap": s["snap_id"], "verdict": "LATE",
                               "note": "同线异族"})
            else:
                # 无人工标注行：safe 区（评审者枚举过=构造存在但语义 safe）或未枚举行
                fp += 1
                detail.append({**i, "snap": s["snap_id"], "verdict": "FP",
                               "why": "评审判定 safe（测试断言/守卫/数值域安全）"})
        for ln, u in unsafe.items():
            if ln not in hit_unsafe:
                fn += 1
                detail.append({**u, "snap": s["snap_id"], "verdict": "FN",
                               "why": "人工标 unsafe 但 scan 未在同线同族命中"})
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    res = {"tp": tp, "fp": fp, "fn": fn, "late": late,
           "precision": round(p, 3), "recall": round(r, 3), "detail": detail}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print(f"TP={tp} FP={fp} FN={fn} LATE={late}  P={p:.3f} R={r:.3f}")
    for d in detail:
        if d["verdict"] in ("FP", "FN", "LATE"):
            print(f"  [{d['verdict']}] {d['snap']} L{d.get('line')} "
                  f"{d.get('rule', d.get('why', ''))}: "
                  f"{(d.get('msg') or d.get('why', ''))[:70]}")
    print("[OK]", os.path.relpath(OUT, ROOT))


if __name__ == "__main__":
    main()
