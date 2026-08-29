# -*- coding: utf-8 -*-
"""h3_score.py —— H3 首测：扫描器在真实对象上的 precision 覆盖与 FP 复检。

组成（EVAL-L2/H3）：
1. 标签面统计 = 复用 l2_score.score()
2. 现场 FP 复检：有案底的误报源（yan-agent 克隆 dsml-tool-call.js ×10 RegExp.exec）
   在当前代码上必须 0 命中——修复回归的硬门槛
3. 规则覆盖实测：VF3 上 ast_scan/bug_scan 的实际规则产出 ⊇ 标签库 rule_expect 家族

用法：python bench/h3_score.py [--json]
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import registry  # noqa: E402
import tools     # noqa: F401,E402
import l2_score  # noqa: E402

YA_CLONE = r"D:\开发\audits\repos\yan-agent-src"
VF3_ROOT = r"D:\开发\VoxelForge-V3"

# 现场可验证家族仅限与目标语言匹配者：VF3 是纯 Rust 仓，
# JS/凭据域规则的命中语义已由合成金样单元测试锁定（tests/test_v2::test_scan_eval_exec…）。
LIVE_RULE_FAMILY = {
    "panic_family": {"rust_panic_macro", "rust_unwrap_expect"},
}


def live_checks():
    out = {}
    js = os.path.join(YA_CLONE, "lib", "dsml-tool-call.js")
    if os.path.exists(js):
        r = registry.call("bug_scan", {"path": js})
        hits = [i for i in (r.get("result") or {}).get("issues", [])
                if i["rule"] == "eval_exec"] if r.get("ok") else None
        found = len(hits) if hits is not None else -1
        out["fp_recheck_eval_exec"] = {
            "file": js, "expected_hits": 0, "found": found,
            "pass": found == 0,
            "note": "案底 FP=10（RegExp.exec 误报），修复后必须保持 0",
        }
    else:
        out["fp_recheck_eval_exec"] = {"skipped": f"审计克隆不存在: {YA_CLONE}"}

    if os.path.exists(VF3_ROOT):
        r = registry.call("ast_scan", {"path": VF3_ROOT, "max_files": 400})
        got = set((r.get("result") or {}).get("by_rule", {}))
        cov = {}
        for fam, rules in LIVE_RULE_FAMILY.items():
            have = sorted(rules & got)
            cov[fam] = {"covered": bool(have), "rules_seen": have}
        cov["note"] = ("JS/凭据域家族命中语义由合成金样 pytest 锁定，"
                       "不做仓库级覆盖断言")
        out["rule_coverage_vf3"] = cov
        rr = (r["result"].get("rust_reach") or {})
        out["bonus_rust_reach"] = rr and rr.get("by_reach")
    else:
        out["rule_coverage_vf3"] = {"skipped": "VF3 不存在"}

    verdicts = []
    for v in out.values():
        if isinstance(v, dict) and v.get("skipped"):
            continue
        if "pass" in v:
            verdicts.append(v["pass"])
        elif v.get("rule_coverage_vf3") is not None:
            pass
    for fam, c in (out.get("rule_coverage_vf3") or {}).items():
        if isinstance(c, dict):
            verdicts.append(c.get("covered") is True)
    out["h3_live_verdict"] = ("PASS" if all(verdicts) else "FAIL") if verdicts else "UNVERIFIABLE"
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    report = {"labels": l2_score.score(), "live": live_checks()}
    if a.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        lv = report["labels"]
        print(f"标签库 {lv['labels_total']} 条 / 排除不可判 {lv['excluded_items']}")
        for rule, s in lv["by_rule"].items():
            print(f"  {rule:16s} tp={s['tp']} n={s['n_judgeable']} "
                  f"precision≈{s['precision']} gate={s['gate']}")
        live = report["live"]
        for k, v in live.items():
            print(f"{k}: {json.dumps(v, ensure_ascii=False)[:150]}")
    out_path = os.path.join(HERE, "results", "h3_report.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    json.dump(report, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[OK] 写入 {os.path.relpath(out_path, os.path.dirname(ROOT))}")


if __name__ == "__main__":
    main()
