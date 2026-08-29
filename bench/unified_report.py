# -*- coding: utf-8 -*-
"""unified_report.py —— T4：三套评测器统一报告（verified 为主、judge 为辅）。

聚合：
  L3  bench/results/l3/summary.json        （VF3 诊断，judge rubric 口径）
  P3  bench/results/swe/summary.json       （SWE-bench 外锚：verified 主 / judge 辅）
  P1  bench/results/p1_summary.json        （bug_scan 标注库 P/R）
  P1m bench/results/p1_manual_pr.json      （独立人工标注 P/R）
输出：bench/results/unified_report.json + 控制台。
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(HERE, "results", "unified_report.json")


def _load(p):
    fp = os.path.join(HERE, "results", p)
    return json.load(open(fp, encoding="utf-8")) if os.path.exists(fp) else None


def _p3_stats():
    """从 result 文件聚合：verified（主）与 judge（辅）。"""
    import glob
    arms = {}
    for fp in glob.glob(os.path.join(HERE, "results", "swe", "*_*.json")):
        if os.path.basename(fp) in ("summary.json", "unified_report.json"):
            continue
        d = json.load(open(fp, encoding="utf-8"))
        arm = d["arm"]
        a = arms.setdefault(arm, {"n": 0, "feasible": 0, "verified": 0,
                                  "base_bad": 0, "j_n": 0, "eq": 0, "root": 0})
        a["n"] += 1
        v = d.get("verify") or {}
        if "skip" not in v:
            a["feasible"] += 1
            if v.get("base_ftb_fail") is False:
                a["base_bad"] += 1
            a["verified"] += int(v.get("verified") is True)
        j = d.get("judge")
        if isinstance(j, dict):
            a["j_n"] += 1
            a["eq"] += int(j.get("fix_equivalent") is True)
            a["root"] += int(j.get("same_root_cause") is True)
    return arms


def main():
    report = {"policy": "verified 为主（真实 fail-to-pass 执行）、judge 为辅（语义等效）",
              "generators": {
                  "L3": "bench/ab_run.py --run/--judge/--score",
                  "P3": "bench/swe_p3.py --run/--judge/--score + "
                        "bench/swe_verify.py --verify + bench/swe_repair.py --run",
                  "P1": "bench/p1_build.py + bench/p1_score.py + bench/p1_score2.py",
              }}
    # P3：执行验证为主
    p3 = {}
    for arm, a in _p3_stats().items():
        p3[arm] = {"n": a["n"], "feasible": a["feasible"],
                   "verified": a["verified"],
                   "verified_pct": round(a["verified"] / max(a["feasible"], 1) * 100, 1),
                   "base_bad": a["base_bad"],
                   "fix_equivalent_pct": round(a["eq"] / max(a["j_n"], 1) * 100, 1),
                   "same_root_pct": round(a["root"] / max(a["j_n"], 1) * 100, 1)}
    report["P3"] = {"protocol": "SWE-bench 外锚：真 checkout + FTB 执行验证（主）+ "
                                "三票 judge 语义等效（辅）", "arms": p3}
    # L3：judge rubric + 环境锚（VF3 cargo test 实跑 = 评测底座可执行性验证）
    l3 = _load("l3/summary.json")
    anchor = _load("l3_env_anchor.json")
    report["L3"] = {"protocol": "VF3 诊断双臂：judge rubric 口径（诊断型任务无补丁"
                                "产物，verified 不适用于答案本身）；环境锚 = VF3 "
                                "cargo test 实跑（评测底座可执行性）",
                    "arms": l3 or {}, "env_verified": anchor}
    # P1
    p1 = _load("p1_summary.json")
    p1m = _load("p1_manual_pr.json")
    report["P1"] = {
        "protocol": "标注库 P/R（自标注口径）+ 独立人工标注审计",
        "corpus_pr": (p1 or {}).get("summary"),
        "manual_audit": ({"tp": (p1m or {}).get("tp"), "fp": (p1m or {}).get("fp"),
                          "fn": (p1m or {}).get("fn"),
                          "note": "clue 家族全量上报为设计使然，FP 按 definite/clue 拆分解读"}
                         if p1m else None),
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print("=== unified report（verified 主 / judge 辅）===")
    for arm, s in sorted(p3.items()):
        print(f"P3[{arm}] n={s['n']} verified={s['verified_pct']}% "
              f"judge_eq={s['fix_equivalent_pct']}% same_root={s['same_root_pct']}%")
    if (p1m or {}).get("tp") is not None:
        print(f"P1 manual-audit TP={p1m['tp']} FP={p1m['fp']} FN={p1m['fn']}")
    print("[OK]", os.path.relpath(OUT, ROOT))


if __name__ == "__main__":
    main()
