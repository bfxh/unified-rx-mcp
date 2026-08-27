# -*- coding: utf-8 -*-
"""h2_guard_eval.py —— H2 首测：hallucination_guard 判定 vs 路径存在性真值的一致率。

数据源：bench/results/l3/** 已收集的答案（本会话双臂实验产物，零额外 API 成本）。
真值口径：与 ab_run.halluc_rate 同一文件存在性检查（VF3_ROOT 下 isfile）。
一致性：guard=refuted ↔ 文件不存在；其余(verified/unverifiable) ↔ 文件存在；
        guard 行号语义更细（存在但行号越界仍判 refuted），此类真值侧计"宽"，单列不扣分。

用法：python bench/h2_guard_eval.py [--arm bare_model]   # 默认双臂都测
"""
import argparse
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import registry  # noqa: E402
import tools     # noqa: F401,E402

VF3_ROOT = r"D:\开发\VoxelForge-V3"
FILE_RE = re.compile(r"([A-Za-z0-9_./\\\-]+\.(?:py|rs|go|ts|js|gd|cs|dart|java|kt|rb|php))"
                     r"(?::([A-Za-z0-9_]+))?")


def truth_exists(path_claim):
    p = path_claim.replace("\\", "/").lstrip("./")
    return os.path.isfile(os.path.join(VF3_ROOT, p))


def evaluate(answers):
    agree = strict_agree = total = 0
    rows = []
    for a in answers:
        r = registry.call_with_context("hallucination_guard", {"text": a, "root": VF3_ROOT},
                                       request_id="h2-eval")
        if not r.get("ok"):
            continue
        for item in r["result"]["results"]:
            if item["kind"] != "file":
                continue
            m = FILE_RE.fullmatch(item["decl"].strip("`"))
            if not m:
                continue
            claim, suffix = m.group(1), m.group(2)
            exists = truth_exists(claim)
            total += 1
            # 宽口径一致：refuted↔不存在；verified/unverifiable↔存在
            ok_wide = (item["status"] == "refuted") == (not exists)
            # 严口径：宽一致且不存在时不依赖行号辩解 / 存在时不得因 suffix 被拒
            ok_strict = ok_wide and (
                exists or (item["status"] == "refuted"
                           and ("不存在" in item["detail"] or not suffix)))
            agree += ok_wide
            strict_agree += ok_strict
            if len(rows) < 400:
                rows.append({"claim": item["decl"], "exists": exists,
                             "guard": item["status"], "wide_ok": ok_wide})
    return {"claims": total,
            "wide_agreement": round(agree / total, 4) if total else None,
            "strict_agreement": round(strict_agree / total, 4) if total else None,
            "samples": rows[:200]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", default=None, help="bare_model/model_plus_rx，默认全部")
    args = ap.parse_args()
    report = {"root": VF3_ROOT, "arms": {}}
    pattern = os.path.join(HERE, "results", "l3", "*", "*", "*.json")
    by_arm = {}
    for fp in sorted(glob.glob(pattern)):
        d = json.load(open(fp, encoding="utf-8"))
        arm = fp.replace("\\", "/").split("/l3/")[1].split("/")[0]
        if args.arm and arm != args.arm:
            continue
        ans = d.get("answer")
        if ans:
            by_arm.setdefault(arm, []).append(ans)
    for arm, answers in sorted(by_arm.items()):
        rep = evaluate(answers)
        report["arms"][arm] = rep
        print(f"{arm:16s} claims={rep['claims']:4d} "
              f"wide={rep['wide_agreement']} strict={rep['strict_agreement']}")
    out = os.path.join(HERE, "results", "l3", "h2_report.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump(report, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[OK] 写入 {os.path.relpath(out, HERE)}")


if __name__ == "__main__":
    main()
