# -*- coding: utf-8 -*-
"""p1_score.py —— P1 首测：bug_scan 在标注库上的 P/R。

定义（诚实口径）：
  bug 条目（15）：父提交态文件，rule_expect 应命中 → 命中=TP，未命中=FN
  clean 条目（15）：文件应零命中 → 任何 issue=FP，零=TN
  P = TP/(TP+FP)  R = TP/(TP+FN)
产出：bench/results/p1_summary.json + 控制台表。
"""
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import registry  # noqa: E402
import tools  # noqa: E402,F401

CORPUS = os.path.join(HERE, "bug_corpus.jsonl")
OUT = os.path.join(HERE, "results", "p1_summary.json")
DEV = r"D:\开发"


def scan_text(src, suffix):
    import tempfile
    d = tempfile.mkdtemp(prefix="p1score")
    fp = os.path.join(d, "f" + suffix)
    with open(fp, "w", encoding="utf-8", newline="") as f:
        f.write(src)
    r = registry.call("bug_scan", {"path": fp})
    return r.get("result", {}).get("issues") or []


def score(rows):
    """纯函数便于测试：rows = [{sample, rule_expect, issues:[rule,...]}]"""
    tp = fn = fp = tn = 0
    per_rule = defaultdict(lambda: {"tp": 0, "fn": 0})
    for r in rows:
        rules = Counter(r["issues"])
        if r["sample"] == "bug":
            if rules.get(r["rule_expect"], 0) > 0:
                tp += 1
                per_rule[r["rule_expect"]]["tp"] += 1
            else:
                fn += 1
                per_rule[r["rule_expect"]]["fn"] += 1
        else:
            if rules:
                fp += 1
            else:
                tn += 1
    p = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    return {"tp": tp, "fn": fn, "fp": fp, "tn": tn,
            "precision": round(p, 3), "recall": round(rec, 3),
            "per_rule": {k: dict(v) for k, v in per_rule.items()}}


def main():
    rows = []
    for line in open(CORPUS, encoding="utf-8"):
        if not line.strip():
            continue
        e = json.loads(line)
        repo, sha = e["repo"], e["parent_sha"] if e["sample"] == "bug" else e["fix_sha"]
        out = subprocess.run(
            ["git", "-C", os.path.join(DEV, repo), "show", f"{sha}:{e['file']}"],
            capture_output=True, timeout=120)
        if out.returncode != 0:
            rows.append({**e, "issues": ["<missing-file>"]})
            continue
        src = out.stdout.decode("utf-8", errors="replace")
        issues = [x["rule"] for x in scan_text(src, os.path.splitext(e["file"])[1])]
        rows.append({**e, "issues": issues})
    s = score(rows)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"summary": s, "rows": rows}, f, ensure_ascii=False, indent=1)
    print(f"TP={s['tp']} FN={s['fn']} FP={s['fp']} TN={s['tn']} "
          f"P={s['precision']:.3f} R={s['recall']:.3f}")
    print("per_rule:", json.dumps(s["per_rule"], ensure_ascii=False))
    print("[OK]", os.path.relpath(OUT, ROOT))


if __name__ == "__main__":
    main()
