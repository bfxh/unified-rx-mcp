# -*- coding: utf-8 -*-
"""ab_run.py —— L3 双臂增益评测实跑器（EVAL-P2 / UPGRADE-S14）

双臂：A=裸模型(API 单轮)   B=模型+只读证据工具集(进程内 registry.call)。
语料：bench/l3_tasks.jsonl（VoxelForge-V3 真实历史缺陷 12 条 × rubric）。
通道：凭据从 Yan Agent 配置直接读取——**永不打印明文 key**。

用法：
  python bench/ab_run.py --run A --n 3                 # A 臂全量
  python bench/ab_run.py --run B --tasks VF3-T01 --n 1 # 冒烟单任务
  python bench/ab_run.py --judge                       # 批量判分（Agent-as-a-Judge）
  python bench/ab_run.py --score                       # 汇总表 + H1 判定 + 路径幻觉率

设计偏离声明（如实记录）：B 臂走进程内 registry.call 而非 stdio 子进程——
同一工具面、同一裁剪语义（_clamp/S10 门禁全生效），省去每请求进程开销；
协议级行为已有 tests/test_protocol*.py 与 S10 e2e 探针背书。
"""
import argparse
import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import registry  # noqa: E402
import tools      # noqa: F401,E402  注册面

CORPUS = os.path.join(HERE, "l3_tasks.jsonl")
RESULTS = os.path.join(HERE, "results", "l3")
YAN_CONFIG = os.environ.get("YAN_CONFIG",
                            r"C:\Users\lbx13\AppData\Roaming\yan-agent\YanData\config.json")
VF3_ROOT = r"D:\开发\VoxelForge-V3"

# B 臂只读证据子集（评测配置，如实在 manifest 里记录）
ARM_B_TOOLS = ["code_search", "engine_query", "kb_query", "fs_list", "fs_read",
               "fs_stat", "bug_scan", "ast_scan", "std_check", "bug_locate",
               "code_context", "project_scan"]
PRICE_PER_MTOK = {"deepseek-chat": (0.27, 1.10)}          # (in,out) $/Mtok 近似公开价
TEMPERATURE = 0.2
HTTP_TIMEOUT = 180
MAX_TOOL_EXEC = 12
TOOL_RESULT_CAP = 3500                                     # 防上下文淹没
BACKOFF_S = (8, 20, 40, 80)                                # 限流/瞬断退避阶梯

# 节流与请求附加体（main 按通道配置；chat 统一消费——run_arm_b/judge 自动继承）
_REQ_GAP = {"v": 0.0}
_EXTRA = {"v": None}

SYS_A = (
    "你是资深 Rust/Bevy 游戏代码诊断专家。根据用户的缺陷现象描述给出严格三段式回答：\n"
    "【根因分析】【定位】（文件路径+符号，格式 `path:symbol`，无证据的标注(推测)；"
    "禁止编造不存在的文件或符号）【修复方案】（分步最小改动，不写完整实现代码）。")

SYS_B = SYS_A + (
    f"\n\n你可调用只读诊断工具检索仓库证据，仓库根：{VF3_ROOT}。"
    "证据确凿的定位不标(推测)。收集足够后必须给出同样的三段式最终文字回答。")


# ---------- 凭据 / HTTP ----------

def load_channel(name):
    cfg = json.load(open(YAN_CONFIG, encoding="utf-8"))
    p = cfg.get("api", {}).get("providerConfigs", {}).get(name)
    if not p or not p.get("apiKey"):
        raise SystemExit(f"[FAIL] 通道 {name} 无凭据（config 检查仅本地，key 不回显）")
    return {"base": p["baseUrl"].rstrip("/"), "key": p["apiKey"]}


def chat(ch, model, messages, tools_schema=None, retries=3, extra=None):
    payload = {"model": model, "messages": messages, "temperature": TEMPERATURE}
    if tools_schema:
        payload["tools"] = [{"type": "function", "function": t} for t in tools_schema]
    if _EXTRA["v"]:
        payload.update(_EXTRA["v"])
    if extra:
        payload.update(extra)
    body = json.dumps(payload).encode()
    last_err = None
    last_detail = ""
    for attempt in range(retries + 1):
        if _REQ_GAP["v"] and attempt == 0:
            time.sleep(_REQ_GAP["v"])
        try:
            req = urllib.request.Request(
                ch["base"] + "/chat/completions", data=body,
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + ch["key"]})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                last_detail = e.read().decode(errors="replace")[:200]
            except Exception:
                last_detail = ""
            last_err = e
            retryable = e.code == 429 or e.code >= 500
            if not retryable or attempt >= retries:
                break
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
            if attempt >= retries:
                break
        time.sleep(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
    kind = f"HTTP{last_err.code}" if isinstance(last_err, urllib.error.HTTPError) \
        else type(last_err).__name__
    raise SystemExit(f"[FAIL] API 连续失败（{kind}）{last_detail[:120]}")


def usage_of(resp):
    u = resp.get("usage") or {}
    return int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))


# ---------- 语料 / 工具面 ----------

def load_tasks(ids=None):
    out = []
    with open(CORPUS, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            t = json.loads(line)
            missing = [k for k in ("id", "commit", "area", "issue", "rubric") if k not in t]
            if missing:
                raise SystemExit(f"[FAIL] 语料缺字段 {missing}: {t.get('id','?')}")
            if ids and t["id"] not in ids:
                continue
            out.append(t)
    if not out:
        raise SystemExit("[FAIL] 语料筛选为空")
    return out


def arm_b_schemas():
    want = set(ARM_B_TOOLS)
    schemas = []
    for t in registry.list_tools():
        if t["name"] in want:
            fn = {"name": t["name"], "description": t.get("description", ""),
                  "parameters": t.get("inputSchema") or {"type": "object"}}
            schemas.append(fn)
    got = {s["name"] for s in schemas}
    miss = want - got
    if miss:
        raise SystemExit(f"[FAIL] ARM_B_TOOLS 中不存在于注册面: {sorted(miss)}")
    return schemas


# ---------- B 臂工具循环 ----------

def exec_tool(name, args):
    r = registry.call_with_context(name, args, request_id=f"ab-{int(time.time()*1000)}")
    txt = json.dumps(r, ensure_ascii=False)
    if len(txt) > TOOL_RESULT_CAP:
        txt = txt[:TOOL_RESULT_CAP] + f"\n…[runner truncated {len(txt)-TOOL_RESULT_CAP} chars]"
    return txt


def run_arm_b(ch, model, task, max_rounds, trace_out):
    msgs = [{"role": "system", "content": SYS_B},
            {"role": "user", "content": task_prompt(task)}]
    schemas = arm_b_schemas()
    usage_in = usage_out = 0
    tool_trace = []
    answer = None
    for rnd in range(max_rounds):
        active_tools = schemas if len(tool_trace) < MAX_TOOL_EXEC else None
        t0 = time.time()
        resp = chat(ch, model, msgs, tools_schema=active_tools)
        i, o = usage_of(resp)
        usage_in += i
        usage_out += o
        msg = (resp.get("choices") or [{}])[0].get("message", {})
        calls = msg.get("tool_calls") or []
        if not calls or not active_tools:
            answer = msg.get("content") or ""
            break
        msgs.append({"role": "assistant",
                     "content": msg.get("content"),
                     "tool_calls": calls})
        for c in calls:
            fn = c.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            tr = exec_tool(fn.get("name", ""), args)
            tool_trace.append({"tool": fn.get("name"), "round": rnd,
                               "ms": None})
            msgs.append({"role": "tool", "tool_call_id": c.get("id"),
                         "content": tr})
        trace_out.append(f"round{rnd}: {len(calls)} calls "
                         f"{sum(len(str(m)) for m in msgs[-len(calls):]):d}ch "
                         f"({time.time()-t0:.1f}s)")
    else:
        msgs.append({"role": "user", "content": "工具轮次已达上限，现在必须直接给最终三段式回答。"})
        resp = chat(ch, model, msgs)
        i, o = usage_of(resp)
        usage_in += i
        usage_out += o
        answer = (resp.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    return answer, usage_in, usage_out, tool_trace


# ---------- 运行 / 记录 ----------

def task_prompt(task):
    return f"[缺陷报告 · 区域：{task['area']}]\n{task['issue']}\n\n请按系统要求给出三段式回答。"


def result_path(arm, tid, idx, channel):
    return os.path.join(RESULTS, arm, channel, f"{tid}_r{idx}.json")


def run(args):
    ch = load_channel(args.channel)
    tasks = load_tasks(set(args.tasks.split(",")) if args.tasks else None)
    n_total = tok_in = tok_out = 0
    t_start = time.time()
    for t in tasks:
        for idx in range(args.n):
            fp = result_path(args.arm, t["id"], idx, args.channel)
            if os.path.exists(fp):
                old = json.load(open(fp, encoding="utf-8"))
                if not old.get("error_run"):
                    print(f"skip {os.path.relpath(fp, HERE)} (已完成)")
                    continue
            rec = {"task_id": t["id"], "arm": ("bare_model" if args.arm == "A"
                                               else "model_plus_rx"),
                   "run_idx": idx, "ts": int(time.time()),
                   "channel": args.channel, "model": args.model,
                   "tools_subset": ARM_B_TOOLS if args.arm == "B" else [],
                   "schema_bytes": sum(len(json.dumps(s)) for s in arm_b_schemas()) if args.arm == "B" else 0}
            notes = []
            t0 = time.time()
            try:
                if args.arm == "A":
                    resp = chat(ch, args.model,
                                [{"role": "system", "content": SYS_A},
                                 {"role": "user", "content": task_prompt(t)}])
                    rec["answer"] = (resp.get("choices") or [{}])[0] \
                        .get("message", {}).get("content") or ""
                    rec["tokens_in"], rec["tokens_out"] = usage_of(resp)
                    rec["turns"] = 1
                else:
                    ans, tin, tout, tr = run_arm_b(ch, args.model, t,
                                                   args.max_rounds, notes)
                    rec["answer"] = ans
                    rec["tokens_in"], rec["tokens_out"] = tin, tout
                    rec["turns"] = len(tr) + 1
                    rec["tool_trace"] = tr
            except SystemExit as e:
                rec.update({"answer": "", "error_run": str(e), "notes": notes})
                _dump(fp, rec)
                print(f"WARN {t['id']} r{idx}: {str(e)[:80]}")
                continue
            pr = PRICE_PER_MTOK.get(args.model)
            rec["cost_usd"] = round((rec["tokens_in"] * pr[0] + rec["tokens_out"] * pr[1]) / 1e6, 5) if pr else None
            rec["walltime_s"] = round(time.time() - t0, 1)
            rec["notes"] = notes
            _dump(fp, rec)
            n_total += 1
            tok_in += rec["tokens_in"]
            tok_out += rec["tokens_out"]
            print(f"done {t['id']} r{idx} turns={rec['turns']} "
                  f"in={rec['tokens_in']} wall={rec['walltime_s']}s")
    print(f"[OK] 新跑 {n_total} 次 | tok_in={tok_in} tok_out={tok_out} | "
          f"{time.time()-t_start:.0f}s")


def _dump(fp, doc):
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    json.dump(doc, open(fp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


# ---------- Judge（Agent-as-a-Judge）----------

def judge_one(ch, model, task, answer, force=False):
    rub_lines = "\n".join(f"- {r['rid']}: 要求={r['requirement']}｜gold参考={r['gold']}"
                          for r in task["rubric"])
    example = json.dumps({r["rid"]: "pass" for r in task["rubric"]}, ensure_ascii=False)
    base = ("你是严格的独立技术评审。对候选答案逐条判 pass/fail/unverifiable。\n"
            f"[任务缺陷]\n{task['issue']}\n\n[候选答案]\n{answer}\n\n[需求点清单]\n{rub_lines}\n"
            f'输出仅一个 JSON 对象，键必须精确为 {json.dumps([r["rid"] for r in task["rubric"]])}，'
            f'值∈{{"pass","fail","unverifiable"}}。输出仅此 JSON：')
    verdict = err = None
    for attempt in range(2):
        prompt = base if attempt == 0 else (
            base + f"\n（你上一次输出不合规格被拒。严格遵守格式，如：{example}）")
        resp = chat(ch, model, [{"role": "user", "content": prompt}])
        txt = (resp.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        try:
            verdict = validate_verdict(parse_verdict(txt), [r["rid"] for r in task["rubric"]])
            break
        except ValueError as e:
            err = e
    if verdict is None:
        raise ValueError(str(err))
    return verdict


def validate_verdict(verdict, expected):
    """结构校验：键集合与值域都不可漂移。"""
    if sorted(verdict) != sorted(expected):
        raise ValueError(f"verdict 键不符: {sorted(verdict)} != {sorted(expected)}")
    bad = [k for k, v in verdict.items() if v not in ("pass", "fail", "unverifiable")]
    if bad:
        raise ValueError(f"verdict 值非法: {bad}")
    return verdict


def parse_verdict(txt):
    """容错解析：取首个平衡 {} JSON。"""
    start = txt.find("{")
    if start < 0:
        raise ValueError("无 JSON 对象")
    depth = 0
    for end in range(start, len(txt)):
        if txt[end] == "{":
            depth += 1
        elif txt[end] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(txt[start:end + 1])
    raise ValueError("JSON 未闭合")


def do_judge(args):
    ch = load_channel(args.channel)
    tasks = {t["id"]: t for t in load_tasks()}
    files = sorted(glob.glob(os.path.join(RESULTS, "*", "*", "*.json")))
    done = fails = 0
    for fp in files:
        d = json.load(open(fp, encoding="utf-8"))
        if d.get("judge") and not args.force:
            continue
        t = tasks.get(d.get("task_id"))
        if not t or not d.get("answer"):
            continue
        try:
            d["judge"] = judge_one(ch, args.model, t, d["answer"], force=args.force)
        except Exception as e:                      # 判分失败如实记录，不静默跳过
            d["judge_error"] = str(e)[:120]
            fails += 1
        else:
            d.pop("judge_error", None)
            done += 1
        _dump(fp, d)
    print(f"[OK] judge 完成 {done}，失败留痕 {fails}")


# ---------- 汇总 ----------

FABRIC_RE = re.compile(r"`([\w\-\\/\.]+\.(?:rs|py|ron|toml|md))[:\w]*`?")


def halluc_rate(answers, root=VF3_ROOT):
    """回答中声称存在的 .rs/.py 文件在仓库里实际存在的比例（越低越幻觉）。"""
    claims = hits = 0
    for a in answers:
        for m in FABRIC_RE.findall(a):
            p = m.replace("\\", "/").lstrip("./")
            claims += 1
            if os.path.exists(os.path.join(root, p)):
                hits += 1
    return (claims, round(hits / claims, 3) if claims else None)


def do_score(args):
    agg = {}
    paths = {}
    for fp in sorted(glob.glob(os.path.join(RESULTS, "*", "*", "*.json"))):
        d = json.load(open(fp, encoding="utf-8"))
        if not isinstance(d.get("judge"), dict):
            continue
        grp = (d["arm"] + "@" + str(d.get("channel", "?")) + "/" +
               str(d.get("model", "?")))
        j = {k: v for k, v in d["judge"].items() if v == "pass"}
        solved = bool(d["judge"]) and all(v == "pass" for v in d["judge"].values())
        a = agg.setdefault(grp, {"n": 0, "solved": 0, "turns": [], "tin": [],
                                 "tout": [], "cost": [], "wall": [],
                                 "pass_items": 0, "items": 0, "uv": 0})
        a["n"] += 1
        a["solved"] += int(solved)
        a["pass_items"] += len(j)
        a["items"] += len(d["judge"])
        a["uv"] += sum(1 for v in d["judge"].values() if v == "unverifiable")
        for k, key in (("turns", "turns"), ("tokens_in", "tin"), ("tokens_out", "tout"),
                       ("cost_usd", "cost"), ("walltime_s", "wall")):
            if d.get(k) is not None:
                a[key].append(d[k])
        if d.get("answer"):
            paths.setdefault(grp, []).append(d["answer"])
    avg = lambda xs: sum(xs) / len(xs) if xs else float("nan")  # noqa: E731
    print(f"{'arm@channel':<40}{'n':>4}{'solved%':>9}{'R点通过率':>10}{'avg_turns':>11}"
          f"{'avg_tin':>10}{'avg_cost$':>11}{'avg_wall':>10}")
    for name, s in sorted(agg.items()):
        n = max(s["n"], 1)
        print(f"{name:<40}{s['n']:>4}{s['solved']/n*100:>8.1f}%"
              f"{s['pass_items']/max(s['items'],1)*100:>9.1f}%"
              f"{avg(s['turns']):>11.1f}{avg(s['tin']):>10.0f}"
              f"{avg(s['cost']):>11.5f}{avg(s['wall']):>10.1f}")
    channels = sorted({n.split("@", 1)[1] for n in agg})
    for chn in channels:
        ba, bb = agg.get("bare_model@" + chn), agg.get("model_plus_rx@" + chn)
        if ba and bb and ba["n"] and bb["n"]:
            gain = bb["solved"] / bb["n"] - ba["solved"] / ba["n"]
            print(f"\n[{chn}] H1 Δsolved = {gain:+.1%} (B-A) | R点 B vs A = "
                  f"{bb['pass_items']/max(bb['items'],1):.2f} vs "
                  f"{ba['pass_items']/max(ba['items'],1):.2f}")
    for name in sorted(paths):
        c, h = halluc_rate(paths[name])
        print(f"path-existence[{name}]: {hits_str(c, h)}")
    _dump(os.path.join(RESULTS, "summary.json"),
          {"generated": int(time.time()), "arms": agg,
           "path_claims": {k: {"claims": len(v), "exist_rate": halluc_rate(v)[1]}
                           for k, v in paths.items()}})
    print("[OK] 汇总写入 bench/results/l3/summary.json")


def hits_str(c, h):
    if not c:
        return "无文件级声明"
    return f"{c} 处文件引用，存在率 {h:.0%}" if h is not None else f"{c} 处引用均无法核验"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="conn-deepseek")
    ap.add_argument("--model", default="deepseek-chat")
    ap.add_argument("--run", choices=["A", "B"])
    ap.add_argument("--tasks", help="逗号分隔 id 过滤")
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--max-rounds", type=int, default=6)
    ap.add_argument("--req-gap", type=float, default=0.0, help="请求间隔节流秒数")
    ap.add_argument("--thinking", choices=["default", "disabled"], default="default",
                    help="GLM 4.5+ 系思考开关（disabled 提速）")
    ap.add_argument("--judge", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--score", action="store_true")
    args = ap.parse_args()

    if args.run:
        args.arm = args.run  # 语义别名
    _REQ_GAP["v"] = float(args.req_gap or 0.0)
    _EXTRA["v"] = {"thinking": {"type": "disabled"}} if args.thinking == "disabled" else None
    # 触发 import 校验（tools 必须成功导入一次以完成注册）
    assert registry.tool_count() > 0
    if args.run:
        run(args)
    elif args.judge:
        do_judge(args)
    elif args.score:
        do_score(args)
    else:
        ts = load_tasks()
        dry = f"CORPUS {len(ts)} 条 | ARM_B_TOOLS {len(ARM_B_TOOLS)} | " \
              f"channel={args.channel}(masked) model={args.model}"
        print(dry)
        print("[OK] 无模式参数 → 仅自检")


if __name__ == "__main__":
    main()
