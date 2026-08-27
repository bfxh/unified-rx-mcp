# -*- coding: utf-8 -*-
"""replay_ab.py —— L3 任务增益评测骨架（UPGRADE-S6 / EVAL-L3）

双臂回放：A=裸模型基线记录, B=模型+unified-rx 工具面。
本骨架不调任何模型 API——只做三件事：
  1. dry-run：校验语料格式 + 打印双臂配置（合并进 CI 的门禁）
  2. record：把一次真实会话的轨迹落盘（turns/tokens/cost/tools 序列）
  3. score ：按 rubric 判分并输出对比报告

用法：
  python bench/replay_ab.py --dry-run
  python bench/replay_ab.py --record results/A_task01.json --arm A --task VF-xxx
  python bench/replay_ab.py --score results/
"""
import argparse
import glob
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.join(HERE, "labeled_bugs.jsonl")

ARM_A = {"name": "bare_model", "tools": []}
ARM_B = {"name": "model_plus_rx", "server": "unified-rx-mcp v2.1", "tools": 39}

REQUIRED_FIELDS = ("id", "commit", "file", "symptom")


def load_corpus():
    tasks = []
    with open(CORPUS, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            missing = [k for k in REQUIRED_FIELDS if k not in rec]
            if missing:
                raise SystemExit(f"[FAIL] 语料缺字段 {missing}: {rec.get('id', '?')}")
            tasks.append(rec)
    return tasks


def dry_run(tasks):
    n_excl = sum(1 for t in tasks if t.get("note", "").startswith("排除") or "排除" in t.get("note", ""))
    print(f"CORPUS OK: {len(tasks)} 条标注（其中 {n_excl} 条标注为『文本规则不可判』对照样本）")
    print(f"ARM A: {ARM_A['name']} (无工具)")
    print(f"ARM B: {ARM_B['name']} ({ARM_B['server']}, {ARM_B['tools']} 工具)")
    print("METRICS: solved_rate | avg_turns | tokens_in/out | cost_usd | walltime_s")
    print("JUDGE  : Agent-as-a-Judge rubric（见 spec/EVAL.md 附模板），抽检率 >=10%")
    for t in tasks:
        arm_eligible = "L2-eligible" if t.get("rule_expect") else "L2-excluded(对照)"
        print(f"  {t['id']} [{arm_eligible}] {t['file']}: {t['symptom'][:44]}...")
    print("[OK] dry-run 通过")


def record(path, arm, task_id):
    """记录一次真实运行轨迹（由外部 harness 或人工粘贴摘要喂进来）。"""
    doc = {
        "task_id": task_id,
        "arm": arm,
        "ts": int(time.time()),
        "turns": None,          # 会话轮次
        "tokens_in": None,      # input tokens 合计
        "tokens_out": None,
        "cost_usd": None,
        "walltime_s": None,
        "tool_trace": [],       # [(tool, args_digest, ms)] 仅 ARM B 有
        "patch_diff": "",       # 模型产出的最终 patch
        "judge": None,          # {R1: pass/fail/unverifiable, ...} 由 score 阶段填
    }
    if os.path.exists(path):
        old = json.load(open(path, encoding="utf-8"))
        old.update({k: v for k, v in doc.items() if k in ("task_id", "arm", "ts")})
        doc = old
    json.dump(doc, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[OK] recorded -> {path}")


def score(results_dir):
    files = sorted(glob.glob(os.path.join(results_dir, "*.json")))
    if not files:
        raise SystemExit("[FAIL] 无结果文件")
    agg = {}
    for fp in files:
        d = json.load(open(fp, encoding="utf-8"))
        if d.get("judge") is None:
            continue
        j = d["judge"]
        solved = all(v == "pass" for v in j.values())
        a = agg.setdefault(d["arm"], {"n": 0, "solved": 0, "turns": [], "tok_in": [], "cost": [], "wall": []})
        a["n"] += 1
        a["solved"] += int(solved)
        for k, key in (("turns", "turns"), ("tokens_in", "tok_in"), ("cost_usd", "cost"), ("walltime_s", "wall")):
            if d.get(k) is not None:
                a[key].append(d[k])
    print(f"{'arm':<16} {'n':>3} {'solved%':>8} {'avg_turns':>10} {'avg_tok_in':>11} {'avg_cost':>9} {'avg_wall':>9}")
    for arm, s in sorted(agg.items()):
        n = max(s["n"], 1)
        avg = lambda xs: (sum(xs) / len(xs)) if xs else float("nan")
        print(f"{arm:<16} {s['n']:>3} {s['solved']/n*100:>7.1f}% {avg(s['turns']):>10.1f} "
              f"{avg(s['tok_in']):>11.0f} {avg(s['cost']):>9.4f} {avg(s['wall']):>9.1f}")
    # H1 判定：B 相对 A 的解决率增益 & token 降幅
    if "bare_model" in agg and "model_plus_rx" in agg:
        ba, bb = agg["bare_model"], agg["model_plus_rx"]
        if ba["n"] and bb["n"]:
            gain = (bb["solved"] / bb["n"]) - (ba["solved"] / ba["n"])
            print(f"\nH1 Δsolved_rate = {gain:+.1%}（B-A；>=0 即假设成立方向）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--record")
    ap.add_argument("--arm", choices=["A", "B"])
    ap.add_argument("--task")
    ap.add_argument("--score")
    args = ap.parse_args()

    tasks = load_corpus()
    if args.dry_run:
        dry_run(tasks)
    elif args.record:
        arm_name = "bare_model" if args.arm == "A" else "model_plus_rx"
        record(args.record, arm_name, args.task or "?")
    elif args.score:
        score(args.score)
    else:
        dry_run(tasks)


if __name__ == "__main__":
    main()
