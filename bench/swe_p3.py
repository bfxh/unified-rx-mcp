# -*- coding: utf-8 -*-
"""swe_p3.py —— P3 外锚首期：SWE-bench Verified 抽样对比协议。

与标准 SWE-bench 的偏离（如实声明，见 spec/EVAL.md P3）：
- 不构建仓库测试环境（Windows 上旧依赖地狱）→ 不执行 fail-to-pass 测试
- 判分 = Agent-as-a-Judge：gold patch 对照候选 patch 判
  same_issue_area / same_root_cause / fix_equivalent（strict JSON）
- 双臂都在【同一份 checkout 的真实仓库】上工作，信息可达性对齐：
    A open-book-manual：只有 fs_list/fs_read/fs_stat（手翻）
    B open-book-tools ：全部只读诊断工具（code_search/ast_scan/ide_lsp/...）
  变量 = 工具强度，而不是"有没有代码"。

用法：
  python bench/swe_p3.py --fetch           # 抽样 6 条 + 克隆/checkout
  python bench/swe_p3.py --run             # 双臂求解
  python bench/swe_p3.py --judge           # gold 对照判分
  python bench/swe_p3.py --score           # 汇总

凭据读取同 ab_run（Yan 配置，零明文回显）。
"""
import argparse
import json
import os
import random
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import ab_run as AB            # noqa: E402  复用 chat/usage/exec_tool/registry
import registry                # noqa: E402
import tools                   # noqa: F401,E402

WORK = os.path.join(os.environ.get("TEMP", "."), "opencode", "swe")
PARQUET = os.path.join(WORK, "verified.parquet")
SAMPLE = os.path.join(HERE, "results", "swe_sample.jsonl")
RESULTS_DIR = os.path.join(HERE, "results", "swe")
MIRROR = "https://hf-mirror.com/datasets/princeton-nlp/SWE-bench_Verified/resolve/main/data/test-00000-of-00001.parquet"

SAMPLE_PLAN = [  # (repo, n) 首轮 6 条均衡跨仓
    ("django/django", 2), ("sympy/sympy", 1), ("sphinx-doc/sphinx", 1),
    ("scikit-learn/scikit-learn", 1), ("psf/requests", 1),
]
MAX_PATCH = 4000
MAX_ISSUE = 3500

ARM_A_TOOLS = ["fs_list", "fs_read", "fs_stat"]
ARM_B_TOOLS = ["code_search", "engine_query", "ast_scan", "bug_locate",
               "code_context", "ide_lsp", "bug_scan", "fs_list", "fs_read", "fs_stat"]

TEMPERATURE = AB.TEMPERATURE


# ---------------- fetch ----------------

def fetch(parquet_exists_ok=True):
    os.makedirs(WORK, exist_ok=True)
    if not (parquet_exists_ok and os.path.exists(PARQUET)):
        req = urllib.request.Request(MIRROR, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=900) as r, open(PARQUET, "wb") as f:
            while True:
                c = r.read(1 << 20)
                if not c:
                    break
                f.write(c)
        print(f"parquet downloaded -> {PARQUET}")
    import duckdb
    con = duckdb.connect()
    conds = " OR ".join([f"repo = '{r}'" for r, _ in SAMPLE_PLAN])
    rows = con.sql(f"""
        SELECT instance_id, repo, base_commit, problem_statement, patch
        FROM read_parquet('{PARQUET}')
        WHERE ({conds}) AND length(patch) < {MAX_PATCH}
          AND length(problem_statement) < {MAX_ISSUE}
    """).fetchall()
    random.seed(20260827)
    picked = []
    for repo, n in SAMPLE_PLAN:
        pool = [x for x in rows if x[1] == repo]
        random.shuffle(pool)
        picked += pool[:n]
    os.makedirs(os.path.dirname(SAMPLE), exist_ok=True)
    with open(SAMPLE, "w", encoding="utf-8") as f:
        for iid, repo, commit, issue, patch in picked:
            f.write(json.dumps({"instance_id": iid, "repo": repo,
                                "base_commit": commit,
                                "issue": issue, "gold_patch": patch},
                               ensure_ascii=False) + "\n")
    print(f"[OK] {len(picked)} 条抽样 -> {os.path.relpath(SAMPLE, ROOT)}", flush=True)
    clone_all(picked)


def _clone_checkout(repo, commit, dst):
    """快照级拉取：init + fetch --depth 1 <sha> + checkout FETCH_HEAD。
    秒级~分钟级；blobless 全量克隆在国内网络上会挂到天荒地老（S19 实测）。"""
    if not os.path.exists(os.path.join(dst, ".git")):
        os.makedirs(dst, exist_ok=True)
        url = f"https://github.com/{repo}.git"
        subprocess.run(["git", "init", dst], check=True, timeout=60,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "-C", dst, "remote", "add", "origin", url],
                       check=True, timeout=60)
        subprocess.run(["git", "-C", dst, "config", "core.longpaths", "true"],
                       timeout=60)
        r = subprocess.run(["git", "-C", dst, "fetch", "--depth", "1", "origin",
                            commit], timeout=420)
        if r.returncode != 0:
            raise RuntimeError(f"fetch sha 失败 rc={r.returncode}")
    subprocess.run(["git", "-C", dst, "checkout", "FETCH_HEAD"], check=True,
                   timeout=240, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return dst


def clone_all(picked=None):
    if picked is None:
        picked = []
        with open(SAMPLE, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    d = json.loads(line)
                    picked.append((d["instance_id"], d["repo"], d["base_commit"],
                                   d.get("issue", ""), d.get("gold_patch", "")))
    for iid, repo, commit, *_ in picked:
        dst = os.path.join(WORK, iid.replace("/", "__"))
        t0 = time.time()
        try:
            _clone_checkout(repo, commit, dst)
            print(f"[OK] checkout {iid} @ {commit[:8]} ({time.time()-t0:.0f}s) -> {dst}")
        except Exception as e:                                # noqa: BLE001
            print(f"[WARN] {iid}: {type(e).__name__} {str(e)[:120]}")


# ---------------- run ----------------

def arm_prompt(instance):
    return (f"[SWE issue · {instance['instance_id']}]\n"
            f"{instance['issue']}\n\n"
            "Produce the fix as a unified diff patch for this repository. "
            "End with a ```diff block only.")


def run_once(arm, inst, ch, model, max_rounds, root, tools_schema):
    if arm == "A":
        sys_p = ("You are fixing a real repository issue with minimal read tools "
                 f"(fs_list/fs_read/fs_stat). Repo root: {root}\n"
                 "Locate the relevant code yourself, then output a unified diff patch. "
                 "End with a ```diff block.")
        msgs = [{"role": "system", "content": sys_p},
                {"role": "user", "content": arm_prompt(inst)}]
    else:
        sys_p = (AB.SYS_B.split("Produce")[0] if False else
                 "You are a senior engineer fixing a real repository issue. "
                 f"Repo root: {root}\n"
                 "Use the read-only diagnostic tools (semantic search, AST scan, "
                 "LSP definition/references, file read) to ground your fix in "
                 "actual code. Then output a unified diff patch, ending with a "
                 "```diff block.")
        msgs = [{"role": "system", "content": sys_p},
                {"role": "user", "content": arm_prompt(inst)}]
    tin = tout = 0
    trace = []
    answer = ""
    for rnd in range(max_rounds):
        active = tools_schema if tools_schema else None   # A/B 臂各带各自的工具面
        resp = AB.chat(ch, model, msgs, tools_schema=active)
        i, o = AB.usage_of(resp)
        tin += i
        tout += o
        m = (resp.get("choices") or [{}])[0].get("message", {})
        calls = m.get("tool_calls") or []
        if not calls or not active:
            answer = m.get("content") or ""
            break
        msgs.append({"role": "assistant", "content": m.get("content"),
                     "tool_calls": calls})
        for c in calls:
            fn = c.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            txt, ms = AB.exec_tool(fn.get("name", ""), args)
            trace.append({"tool": fn.get("name"), "round": rnd, "ms": ms})
            msgs.append({"role": "tool", "tool_call_id": c.get("id"), "content": txt})
    else:
        msgs.append({"role": "user",
                     "content": "No more tool calls allowed. Your NEXT message must be "
                                "ONLY the final unified diff inside a ```diff fenced "
                                "block — no tool markup, no prose."})
        resp = chat_retry(ch, model, msgs)
        i, o = AB.usage_of(resp)
        tin += i
        tout += o
        answer = (resp.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    # 提炼兜底：收线回复若仍是工具调用残留/无 diff 块，单独再要一次纯 diff
    if "```diff" not in answer:
        msgs.append({"role": "assistant", "content": answer[:2000]})
        msgs.append({"role": "user",
                     "content": "Your previous reply contained tool-call markup instead "
                                "of a patch. Output ONLY the final unified diff in one "
                                "```diff fenced block, nothing else."})
        resp = chat_retry(ch, model, msgs)
        i, o = AB.usage_of(resp)
        tin += i
        tout += o
        answer2 = (resp.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        if "```diff" in answer2 or len(answer2) > len(answer):
            answer = answer2
    return answer, tin, tout, trace


def chat_retry(ch, model, msgs):
    try:
        return AB.chat(ch, model, msgs)
    except SystemExit as e:
        return {"choices": [{"message": {"content": f"(API FAIL) {e}"}}]}


def run(args):
    ch = AB.load_channel(args.channel)
    insts = load_sample()
    schemas_a = schemas_for(ARM_A_TOOLS)
    schemas_b = schemas_for(ARM_B_TOOLS)
    for inst in insts:
        root = os.path.join(WORK, inst["instance_id"].replace("/", "__"))
        if not os.path.isdir(root):
            print(f"[WARN] no checkout for {inst['instance_id']}（先 --fetch）")
            continue
        for arm, schemas in (("A", schemas_a), ("B", schemas_b)):
            fp = os.path.join(RESULTS_DIR, f"{inst['instance_id'].replace('/', '__')}_{arm}.json")
            if os.path.exists(fp):
                print("skip", os.path.basename(fp))
                continue
            t0 = time.time()
            ans, tin, tout, trace = run_once(arm, inst, ch, args.model,
                                             args.max_rounds, root, schemas)
            rec = {"instance_id": inst["instance_id"], "arm": arm,
                   "model": args.model, "root": root,
                   "tokens_in": tin, "tokens_out": tout,
                   "turns": len(trace) + 1, "tool_trace": trace,
                   "walltime_s": round(time.time() - t0, 1),
                   "answer": ans}
            os.makedirs(RESULTS_DIR, exist_ok=True)
            json.dump(rec, open(fp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            print(f"done {inst['instance_id']} [{arm}] in={tin} wall={rec['walltime_s']}s")


def schemas_for(names):
    want = set(names)
    out = []
    for t in registry.list_tools():
        if t["name"] in want:
            out.append({"name": t["name"], "description": t.get("description", ""),
                        "parameters": t.get("inputSchema") or {"type": "object"}})
    miss = want - {s["name"] for s in out}
    if miss:
        raise SystemExit(f"[FAIL] 缺工具: {sorted(miss)}")
    return out


def load_sample():
    out = []
    with open(SAMPLE, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
    if not out:
        raise SystemExit("[FAIL] 抽样为空（先 --fetch）")
    return out


# ---------------- judge ----------------

def do_judge(args):
    ch = AB.load_channel(args.channel)
    gold = {d["instance_id"]: d for d in load_sample()}
    import glob
    for fp in sorted(glob.glob(os.path.join(RESULTS_DIR, "*_A.json")) +
                     glob.glob(os.path.join(RESULTS_DIR, "*_B.json"))):
        d = json.load(open(fp, encoding="utf-8"))
        if "judge" in d and not args.force:
            continue
        iid = d["instance_id"]
        inst = gold.get(iid)
        if not inst:
            continue
        prompt = (
            "You are a strict release manager judging whether a candidate patch "
            "correctly fixes the same problem as the reference (gold) patch.\n\n"
            f"[ISSUE]\n{inst['issue'][:3500]}\n\n"
            f"[GOLD PATCH]\n{inst['gold_patch'][:4000]}\n\n"
            f"[CANDIDATE PATCH]\n{(d.get('answer') or '')[:4000]}\n\n"
            'Reply ONLY with JSON: {"same_issue_area": true/false, '
            '"same_root_cause": true/false, "fix_equivalent": true/false, '
            '"reason": "<=40 words"}')
        try:
            verdict = AB.parse_verdict((AB.chat(ch, args.model, [
                {"role": "user", "content": prompt}]) or {})
                .get("choices", [{}])[0].get("message", {}).get("content", ""))
        except Exception as e:                                # noqa: BLE001
            d["judge_error"] = str(e)[:120]
        else:
            d["judge"] = verdict
            d.pop("judge_error", None)
        json.dump(d, open(fp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("judged", os.path.basename(fp))
    print("[OK] judge done")


# ---------------- score ----------------

def do_score():
    import glob
    agg = {}
    for fp in sorted(glob.glob(os.path.join(RESULTS_DIR, "*.json"))):
        if os.path.basename(fp) == "summary.json":
            continue
        d = json.load(open(fp, encoding="utf-8"))
        j = d.get("judge")
        if not isinstance(j, dict):
            continue
        a = agg.setdefault(d["arm"], {"n": 0, "eq": 0, "root": 0,
                                      "tin": [], "wall": []})
        a["n"] += 1
        a["eq"] += int(j.get("fix_equivalent") is True)
        a["root"] += int(j.get("same_root_cause") is True)
        for k, bucket in (("tokens_in", "tin"), ("walltime_s", "wall")):
            if d.get(k) is not None:
                a[bucket].append(d[k])
    print(f"{'arm':<4}{'n':>3}{'fix_equiv%':>11}{'same_root%':>11}{'avg_tin':>10}{'avg_wall':>10}")
    for name, s in sorted(agg.items()):
        n = max(s["n"], 1)
        print(f"{name:<4}{s['n']:>3}{s['eq']/n*100:>10.1f}%{s['root']/n*100:>10.1f}%"
              f"{(sum(s['tin'])/len(s['tin']) if s['tin'] else float('nan')):>10.0f}"
              f"{(sum(s['wall'])/len(s['wall']) if s['wall'] else float('nan')):>10.1f}")
    out = os.path.join(RESULTS_DIR, "summary.json")
    json.dump(agg, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("[OK]", os.path.relpath(out, ROOT))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="conn-deepseek")
    ap.add_argument("--model", default="deepseek-chat")
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--judge", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--max-rounds", type=int, default=6)
    a = ap.parse_args()
    assert registry.tool_count() > 0
    if a.fetch:
        fetch()
    if a.run:
        run(a)
    if a.judge:
        do_judge(a)
    if a.score:
        do_score()
    if not any((a.fetch, a.run, a.judge, a.score)):
        print(__doc__)


if __name__ == "__main__":
    main()
