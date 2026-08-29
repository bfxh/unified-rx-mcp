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
  python bench/swe_p3.py --fetch           # 抽样 + 克隆/checkout（幂等，对齐 SAMPLE_PLAN）
  python bench/swe_p3.py --run             # 双臂求解
  python bench/swe_p3.py --judge           # gold 对照判分
  python bench/swe_p3.py --score           # 汇总

凭据读取同 ab_run（Yan 配置，零明文回显）。
"""
import argparse
import json
import os
import random
import re
import shutil
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

SAMPLE_PLAN = [  # (repo, n) —— S22 扩到 ~50 档（47 条）；n = 每仓目标总量，fetch 幂等
    ("django/django", 8), ("sympy/sympy", 7), ("scikit-learn/scikit-learn", 7),
    ("psf/requests", 5), ("sphinx-doc/sphinx", 5), ("matplotlib/matplotlib", 4),
    ("astropy/astropy", 2), ("pydata/xarray", 2), ("pytest-dev/pytest", 2),
    ("pylint-dev/pylint", 2), ("mwaskom/seaborn", 2), ("pallets/flask", 1),
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
    # n = 每仓目标总量（不是增量）：存量超目标按文件序裁掉、不足则补抽 → fetch 幂等。
    # （S22 教训：此前把 n 当增量连跑三次攒出 47 条，语义已修正，存量全数保留）
    old = []
    if os.path.exists(SAMPLE):
        with open(SAMPLE, encoding="utf-8") as f:
            old = [json.loads(l) for l in f if l.strip()]
    random.seed(20260827)
    final, seen = [], set()
    for repo, n in SAMPLE_PLAN:
        keep = [d for d in old if d["repo"] == repo][:n]
        final += keep
        seen |= {d["instance_id"] for d in keep}
    for repo, n in SAMPLE_PLAN:
        cur = sum(1 for d in final if d["repo"] == repo)
        pool = [x for x in rows if x[1] == repo and x[0] not in seen]
        random.shuffle(pool)
        take = pool[:max(0, n - cur)]
        seen |= {x[0] for x in take}
        final += [{"instance_id": x[0], "repo": x[1], "base_commit": x[2],
                   "issue": x[3], "gold_patch": x[4]} for x in take]
    with open(SAMPLE, "w", encoding="utf-8") as f:
        for d in final:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    print(f"[OK] {len(final)} 条抽样（新增 {len(final)-len(old)}）"
          f" -> {os.path.relpath(SAMPLE, ROOT)}", flush=True)
    clone_all([(d["instance_id"], d["repo"], d["base_commit"]) for d in final])


def _clone_checkout(repo, commit, dst):
    """快照级拉取：init + fetch --depth 1 <sha> + checkout FETCH_HEAD。
    秒级~分钟级；blobless 全量克隆在国内网络上会挂到天荒地老（S19 实测）。
    已有 .git 先试直接 checkout；失败（半初始化/FETCH_HEAD 缺失）推倒重来 ——
    S22 教训：仅凭 .git 存在就跳过 fetch，会把坏仓永久卡死在 checkout 上。"""
    if os.path.exists(os.path.join(dst, ".git")):
        try:
            subprocess.run(["git", "-C", dst, "checkout", "FETCH_HEAD"], check=True,
                           timeout=240, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            return dst
        except Exception:                                 # noqa: BLE001
            shutil.rmtree(dst, ignore_errors=True)
    os.makedirs(dst, exist_ok=True)
    url = f"https://github.com/{repo}.git"
    subprocess.run(["git", "init", dst], check=True, timeout=60,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "-C", dst, "remote", "add", "origin", url],
                   check=True, timeout=60)
    subprocess.run(["git", "-C", dst, "config", "core.longpaths", "true"],
                   timeout=60)
    r = subprocess.run(["git", "-C", dst, "fetch", "--depth", "1", "origin",
                        commit], timeout=1500)
    if r.returncode != 0:
        raise RuntimeError(f"fetch sha 失败 rc={r.returncode}")
    subprocess.run(["git", "-C", dst, "checkout", "FETCH_HEAD"], check=True,
                   timeout=240, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return dst


def _clear_locks(dst):
    """被 timeout 杀掉的 git 可能留下 .lock，重试前清掉（S22 实测无锁也能秒败，纯保险）。"""
    gd = os.path.join(dst, ".git")
    if not os.path.isdir(gd):
        return
    for root, _, files in os.walk(gd):
        for f in files:
            if f.endswith(".lock"):
                try:
                    os.remove(os.path.join(root, f))
                except OSError:
                    pass


def clone_all(picked=None, workers=4):
    if picked is None:
        picked = []
        with open(SAMPLE, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    d = json.loads(line)
                    picked.append((d["instance_id"], d["repo"], d["base_commit"],
                                   d.get("issue", ""), d.get("gold_patch", "")))
    from concurrent.futures import ThreadPoolExecutor

    def job(item):
        iid, repo, commit = item[0], item[1], item[2]
        dst = os.path.join(WORK, iid.replace("/", "__"))
        t0 = time.time()
        for attempt in (1, 2, 3):
            try:
                _clear_locks(dst)
                _clone_checkout(repo, commit, dst)
                print(f"[OK] checkout {iid} @ {commit[:8]} "
                      f"({time.time()-t0:.0f}s) -> {dst}", flush=True)
                return True
            except Exception as e:                # noqa: BLE001
                print(f"[WARN] {iid} attempt{attempt}: {type(e).__name__} "
                      f"{str(e)[:100]}", flush=True)
                time.sleep(3 * attempt)
        print(f"[FAIL] {iid} 放弃（三轮超时）", flush=True)
        return False

    with ThreadPoolExecutor(max_workers=workers) as ex:
        oks = list(ex.map(job, picked))
    print(f"[OK] checkout 完成 {sum(oks)}/{len(picked)}", flush=True)


# ---------------- 机械层：DSML 残片回收 + patch 校验（S23） ----------------

_DSML = r"\uff5c+"                  # DeepSeek 特殊 token 分隔符：全角竖线 ×1~2（实跑两种都出现）
_RE_DSML_INVOKE = re.compile(
    rf'<{_DSML}DSML{_DSML}invoke\s+name="([^"]+)"\s*>(.*?)</{_DSML}DSML{_DSML}invoke>',
    re.S)
_RE_DSML_PARAM = re.compile(
    rf'<{_DSML}DSML{_DSML}parameter\s+name="([^"]+)"\s+string="(true|false)"[^>]*>'
    rf'(.*?)</{_DSML}DSML{_DSML}parameter>', re.S)
_RE_DIFF_FENCE = re.compile(r"```diff\s*\n(.*?)```", re.S)


def safe_join(root, path):
    """模型可控路径 → root 内绝对路径；绝对路径/穿越一律 None（S29 高压）。"""
    if not path or len(path) > 400:
        return None
    p = path.replace("\\", "/").lstrip("/")
    if re.match(r"^[A-Za-z]:", p) or p.startswith("//"):
        return None
    cand = os.path.abspath(os.path.join(root, *p.split("/")))
    root_abs = os.path.abspath(root)
    try:
        if os.path.commonpath([cand, root_abs]) != root_abs:
            return None
    except ValueError:
        return None
    return cand


def parse_dsml(content):
    """API 面 tool_calls 为空但 content 里续写了 DSML 风格工具调用 → 解析。
    S22 实测 8/94 run（A1/B7）以此收场：整轮变死轮，答案只剩标记垃圾。"""
    out = []
    for m in _RE_DSML_INVOKE.finditer(content or ""):
        args = {}
        for p in _RE_DSML_PARAM.finditer(m.group(2)):
            name, is_str, raw = p.group(1), p.group(2) == "true", p.group(3)
            if is_str:
                args[name] = raw
            else:
                try:
                    args[name] = json.loads(raw)
                except (ValueError, TypeError):
                    args[name] = raw
        out.append((m.group(1), args))
    return out


def extract_patch(answer):
    """取最后一个 ```diff 块（模型常先草稿后终稿）；无围栏裸 diff 兜底。
    尾部必须保住一个换行：git apply 对无尾换行 patch 直接 corrupt（自测咬出）。"""
    if not answer:
        return ""
    blocks = _RE_DIFF_FENCE.findall(answer)
    if blocks:
        s = blocks[-1].strip()
    else:
        idx = answer.find("diff --git")
        if idx < 0:
            return ""
        s = answer[idx:idx + MAX_PATCH].strip()
    return s if s.endswith("\n") else s + "\n"


def patch_check(root, patch):
    """git apply --check 机械校验（stdin，不落盘）。返回 (ok, strategy, err)。"""
    if not patch or "@@" not in patch:
        return False, "", "empty-or-no-hunk"
    err = ""
    for strat, extra in (("plain", []), ("ignore-ws", ["--ignore-whitespace"]),
                         ("p0", ["-p0"]), ("ctx1", ["-C1"])):
        r = subprocess.run(
            ["git", "-C", root, "apply", "--check", "--whitespace=nowarn"] + extra,
            input=patch.encode("utf-8"), capture_output=True, timeout=120)
        if r.returncode == 0:
            return True, strat, ""
        err = (r.stderr or b"")[:300].decode(errors="replace")
    return False, "", err


_SR_RE = re.compile(r"```sr\s*\n(.*?)```", re.S)
_SR_PATH = re.compile(r"^\s*path:\s*(\S+)\s*$", re.M)
_SR_BODY = re.compile(
    r"^<{5,}\s*SEARCH\s*\n(.*?)\n^={5,}\s*\n(.*?)\n^>{5,}\s*REPLACE\s*$",
    re.S | re.M)


def arm_prompt(instance):
    sr = (
        "Produce the fix as one or more search/replace blocks. For EACH edit "
        "output exactly:\n"
        "```sr\n"
        "path: relative/path/from/repo/root.py\n"
        "<<<<<<< SEARCH\n"
        "<exact lines copied verbatim from the current file>\n"
        "=======\n"
        "<replacement lines>\n"
        ">>>>>>> REPLACE\n"
        "```\n"
        "SEARCH must match the file content exactly (whitespace included) and "
        "be unique in the file. Copy SEARCH lines verbatim from file content you "
        "have read with tools in this conversation — never from memory. "
        "Multiple blocks are allowed.")
    return (f"[SWE issue · {instance['instance_id']}]\n"
            f"{instance['issue']}\n\n{sr}")


def parse_sr(answer):
    """```sr 块 → [(path, search, replace)]；块内缺 path/标记的整块丢弃并计数。"""
    out = []
    for m in _SR_RE.finditer(answer or ""):
        body = m.group(1)
        pm = _SR_PATH.search(body)
        bm = _SR_BODY.search(body)
        if not (pm and bm):
            continue
        out.append((pm.group(1), bm.group(1), bm.group(2)))
    return out


def _fuzzy_best(txt, s):
    """行级滑窗最优相似度（字符级加权）。返回 (index, ratio, second_ratio)。"""
    import difflib
    lines = txt.split("\n")
    a = s.rstrip("\n").split("\n")
    w = len(a)
    if w == 0 or len(lines) < w:
        return None
    stripped = [l.strip() for l in lines]
    a_stripped = [l.strip() for l in a]
    best, second = None, 0.0
    for i in range(len(lines) - w + 1):
        q = sum(1 for x, y in zip(a_stripped, stripped[i:i + w]) if x == y)
        if q >= max(1, int(w * 0.5)):
            ratio = sum(difflib.SequenceMatcher(None, x, y).ratio()
                        for x, y in zip(a, lines[i:i + w])) / w
            if best is None or ratio > best[1]:
                second = best[1] if best else second
                best = (i, ratio)
    if best is None:
        return None
    return best[0], best[1], second


GROUND_CTX = 40        # 修复轮接地片段：最优窗上下各扩的行数


def apply_sr(root, blocks):
    """SEARCH 精确匹配（容忍 CRLF）→ 模糊窗（>=0.8 且唯一最优）→ 都败则返回
    接地片段供修复轮喂真实文件内容。返回 (applied, fails, diff, fuzzy_n, grounds)。
    应用后取 git diff 作为真 patch，再 checkout 还原保持幂等。"""
    applied, fails, touched, fuzzy_n = 0, [], [], 0
    grounds = {}
    for path, s, r in blocks:
        fp = safe_join(root, path)
        if fp is None:
            fails.append(f"{path}: path-escape-rejected")
            continue
        try:
            with open(fp, encoding="utf-8", errors="replace") as f:
                raw = f.read()
        except OSError:
            fails.append(f"{path}: file-not-found")
            continue
        crlf = "\r\n" in raw
        txt = raw.replace("\r\n", "\n") if crlf else raw
        s_n = s.replace("\r\n", "\n")
        r_n = r.replace("\r\n", "\n")
        new_txt, how = None, ""
        cnt = txt.count(s_n)
        best = _fuzzy_best(txt, s_n) if cnt == 0 else None
        if cnt == 1:
            new_txt, how = txt.replace(s_n, r_n, 1), "exact"
        elif best and best[1] >= 0.8 and best[1] > best[2] + 0.02:
            i, ratio = best[0], best[1]
            lines = txt.split("\n")
            w = len(s_n.rstrip("\n").split("\n"))
            repl = r_n.rstrip("\n").split("\n")
            new_txt = "\n".join(lines[:i] + repl + lines[i + w:])
            how = f"fuzzy({ratio:.2f})"
        elif cnt > 1:
            fails.append(f"{path}: search-ambiguous(x{cnt})")
            continue
        else:
            fails.append(f"{path}: search-not-found(best={best[1]:.2f})" if best
                         else f"{path}: search-not-found")
            if best and best[1] >= 0.3:
                lines = txt.split("\n")
                lo = max(0, best[0] - GROUND_CTX)
                hi = min(len(lines), best[0] + len(s_n.rstrip("\n").split("\n"))
                         + GROUND_CTX)
                grounds[path] = "\n".join(lines[lo:hi])[:3500]
            continue
        with open(fp, "w", encoding="utf-8", newline="") as f:
            f.write(new_txt.replace("\n", "\r\n") if crlf else new_txt)
        applied += 1
        touched.append(path)
        if how.startswith("fuzzy"):
            fuzzy_n += 1
    diff = ""
    if touched:
        r_ = subprocess.run(["git", "-C", root, "diff", "--"] + touched,
                            capture_output=True, timeout=120)
        diff = r_.stdout.decode(errors="replace")
        subprocess.run(["git", "-C", root, "checkout", "--"] + touched,
                       capture_output=True, timeout=120)
    return applied, fails, diff, fuzzy_n, grounds


# ---------------- run ----------------

def run_once(arm, inst, ch, model, max_rounds, root, tools_schema):
    if arm == "A":
        sys_p = ("You are fixing a real repository issue with minimal read tools "
                 f"(fs_list/fs_read/fs_stat). Repo root: {root}\n"
                 "Locate the relevant code yourself, then output search/replace "
                 "edit blocks as instructed.")
        msgs = [{"role": "system", "content": sys_p},
                {"role": "user", "content": arm_prompt(inst)}]
    else:
        sys_p = (AB.SYS_B.split("Produce")[0] if False else
                 "You are a senior engineer fixing a real repository issue. "
                 f"Repo root: {root}\n"
                 "Use the read-only diagnostic tools (semantic search, AST scan, "
                 "LSP definition/references, file read) to ground your fix in "
                 "actual code. Then output search/replace edit blocks as instructed.")
        msgs = [{"role": "system", "content": sys_p},
                {"role": "user", "content": arm_prompt(inst)}]
    tin = tout = 0
    trace = []
    answer = ""
    mech = {"dsml_recovered": 0, "patch_repaired": False}
    for rnd in range(max_rounds):
        active = tools_schema if tools_schema else None   # A/B 臂各带各自的工具面
        resp = AB.chat(ch, model, msgs, tools_schema=active)
        i, o = AB.usage_of(resp)
        tin += i
        tout += o
        m = (resp.get("choices") or [{}])[0].get("message", {})
        calls = m.get("tool_calls") or []
        content = m.get("content") or ""
        if not calls and active:
            dsml = parse_dsml(content)
            if dsml:                     # 文本残片工具调用：就地执行，回收死轮
                msgs.append({"role": "assistant", "content": content})
                outs = []
                for fn_name, fargs in dsml:
                    txt, ms = AB.exec_tool(fn_name, fargs)
                    trace.append({"tool": fn_name, "round": rnd, "ms": ms,
                                  "dsml": True})
                    outs.append(f"$ {fn_name} {json.dumps(fargs, ensure_ascii=False)[:200]}\n{txt}")
                mech["dsml_recovered"] += 1
                msgs.append({"role": "user", "content":
                             "[tool results]\n" + "\n\n".join(outs)[:20000] +
                             "\n\nContinue. When done, output ONLY the final "
                             "search/replace edit blocks (```sr)."})
                continue
        if not calls or not active:
            answer = content
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
                                "ONLY the final search/replace edit blocks (```sr) — "
                                "no tool markup, no prose."})
        resp = chat_retry(ch, model, msgs)
        i, o = AB.usage_of(resp)
        tin += i
        tout += o
        answer = (resp.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    # ---------- 收线机械管道（S23） ----------
    # 证据链：LLM 手写 unified diff 的 hunk 行数不可靠（16/16 "solved" 补丁 0 可
    # 应用）→ S/R 块由 runner 应用、git 生成真 diff；SEARCH 漂移 → 模糊窗兜底 +
    # 失败轮注入真实文件片段；终轮 DSML 残片 → 就地执行；完全无块 → 定位+接地定稿。
    mech.update({"protocol": "none", "patch_ok": False, "patch_strategy": "",
                 "sr_blocks": 0, "sr_applied": 0, "sr_failed": [], "sr_fuzzy": 0,
                 "dsml_final_recovered": 0, "grounded_author": False,
                 "patch_repaired": False})

    def _exec_dsml(content, rnd):
        """执行 content 里的 DSML 文本工具调用，返回结果文本列表。"""
        outs = []
        for fn_name, fargs in parse_dsml(content):
            txt, ms = AB.exec_tool(fn_name, fargs)
            trace.append({"tool": fn_name, "round": rnd, "ms": ms, "dsml": True})
            outs.append(f"$ {fn_name} {json.dumps(fargs, ensure_ascii=False)[:200]}\n{txt}")
        return outs

    def _ask(ask, exec_current=False):
        """管道轮提问；exec_current=先执行当前答案里的 DSML 调用；响应若仍为
        DSML 则执行后按原指令重试一次。"""
        nonlocal answer, tin, tout

        def _send(user_text):
            nonlocal answer, tin, tout
            msgs.append({"role": "assistant", "content": answer[:2000]})
            msgs.append({"role": "user", "content": user_text})
            resp = chat_retry(ch, model, msgs)
            i, o = AB.usage_of(resp)
            tin += i
            tout += o
            answer = ((resp.get("choices") or [{}])[0].get("message", {}).get("content")
                      or "")

        outs = _exec_dsml(answer, "pipe") if exec_current else []
        pre = "[tool results]\n" + "\n\n".join(outs)[:20000] + "\n\n" if outs else ""
        if outs:
            mech["dsml_final_recovered"] += 1
        _send(pre + ask)
        if parse_dsml(answer):               # 答复仍是工具残片：执行后重试一次
            outs = _exec_dsml(answer, "pipe")
            mech["dsml_final_recovered"] += 1
            _send("[tool results]\n" + "\n\n".join(outs)[:20000] +
                  "\n\n" + ask)
        return answer

    blocks = parse_sr(answer)
    diff = extract_patch(answer)

    # (a) 终轮 DSML 残片：就地执行后再索取 S/R 块
    if not blocks and not diff and parse_dsml(answer):
        _ask("Now output ONLY the final search/replace edit blocks (```sr).",
             exec_current=True)
        blocks = parse_sr(answer)
        diff = extract_patch(answer)

    # (b) 完全无块：定位轮 + 真实文件内容注入的接地定稿
    if not blocks and not diff:
        loc = _ask("List ONLY the relative file path(s) you intend to modify, one "
                   "per line in the form `path: <relpath>`, nothing else.")

        def _path_candidates(text):
            pths = re.findall(r"^\s*path:\s*(\S+)\s*$", text or "", re.M)
            pths += re.findall(r"[A-Za-z0-9_./\\-]+\.[A-Za-z0-9]{1,5}", text or "")
            out = []
            for p in pths:
                p = p.replace("\\", "/").lstrip("/").strip(".,`;*\"'()[]")
                if p in out or len(p) > 200 or "/" in p and p.startswith("/"):
                    continue
                if safe_join(root, p) and os.path.isfile(safe_join(root, p)):
                    out.append(p)
                if len(out) >= 3:
                    break
            return out

        paths = _path_candidates(loc)
        if not paths:
            paths = _path_candidates(answer)   # 原始答案里提到的文件也试
        parts = []
        for p in paths:
            fp = safe_join(root, p)
            if fp is None:
                continue
            try:
                c = open(fp, encoding="utf-8",
                         errors="replace").read().replace("\r\n", "\n")
            except OSError:
                continue
            if len(c) > 12000:
                c = c[:6000] + "\n…[middle truncated]…\n" + c[-6000:]
            parts.append(f"===== ACTUAL CONTENT of {p} =====\n{c}")
        if parts:
            _ask("Above are the ACTUAL file contents. Output ONLY the final "
                 "search/replace edit blocks (```sr) — COPY SEARCH lines verbatim "
                 "from these contents.\n" + "\n\n".join(parts)[:26000])
            blocks = parse_sr(answer)
            mech["grounded_author"] = bool(blocks)

    # (c) S/R 应用：精确匹配 → 模糊窗 → 接地修复轮
    if blocks:
        mech["protocol"] = "sr"
        mech["sr_blocks"] = len(blocks)
        sr_answer = answer
        applied, fails, gdiff, fz, grounds = apply_sr(root, blocks)
        fuzzy_n = fz
        if fails:
            listing = "\n".join(f"- {f}" for f in fails[:8])
            ground_txt = "\n\n".join(
                f"[ACTUAL CONTENT of {p} (lines around the closest match)]:\n"
                f"{excerpt}" for p, excerpt in list(grounds.items())[:3])
            _ask("Some search/replace blocks FAILED to apply:\n" + listing[:1200] +
                 ("\n\n" + ground_txt[:9000] if ground_txt else "") +
                 "\n\nRe-output ONLY corrected search/replace blocks:\n"
                 "```sr\npath: <relpath>\n<<<<<<< SEARCH\n<exact lines>\n=======\n"
                 "<replacement>\n>>>>>>> REPLACE\n```\n"
                 "COPY the SEARCH lines verbatim from the actual content above or "
                 "from files you read with tools — never from memory.")
            if parse_sr(answer):
                sr_answer = answer
            b2 = parse_sr(answer)
            if b2:
                applied, fails, gdiff2, fz2, _ = apply_sr(root, b2)
                if gdiff2.strip():
                    gdiff = gdiff2
                fuzzy_n += fz2
            mech["patch_repaired"] = True
            if applied == 0 and sr_answer:
                answer = sr_answer          # 修复轮没产出块时保住原有尝试供判官看
        mech["sr_applied"] = applied
        mech["sr_fuzzy"] = fuzzy_n
        mech["sr_failed"] = fails[:8]
        if gdiff.strip():
            mech["patch_ok"] = True
            mech["patch_strategy"] = "sr"
        diff = gdiff or diff
    # (d) ```diff 兜底（模型无视 S/R 指令时）
    elif diff:
        mech["protocol"] = "diff"
        ok, strat, err = patch_check(root, diff)
        if not ok:
            diff_answer = answer
            _ask("Your patch does NOT apply to the repository. "
                 "`git apply --check` says:\n" + err[:600] +
                 "\nFix the diff (usually wrong context lines or wrong file path), "
                 "or better: re-output as search/replace blocks (```sr).")
            b2 = parse_sr(answer)
            d2 = extract_patch(answer)
            if b2:
                _, _, gdiff, _, _ = apply_sr(root, b2)
                if gdiff.strip():
                    diff, ok, strat = gdiff, True, "sr"
            elif d2:
                ok2, strat2, _ = patch_check(root, d2)
                if ok2 or len(d2) > len(diff):
                    diff, ok, strat = d2, ok2, strat2
            if not ok:
                answer = diff_answer
            mech["patch_repaired"] = True
        mech["patch_ok"] = ok
        mech["patch_strategy"] = strat
    mech["candidate_diff"] = diff[:MAX_PATCH]
    return answer, tin, tout, trace, mech


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
    if args.redo_ids:
        want = {x.strip() for x in args.redo_ids.split(",") if x.strip()}
        for inst in insts:
            if inst["instance_id"] not in want:
                continue
            for arm in args.redo_arms or "AB":
                fp = os.path.join(RESULTS_DIR,
                                  f"{inst['instance_id'].replace('/', '__')}_{arm}.json")
                if os.path.exists(fp):
                    os.remove(fp)
                    print("redo", os.path.basename(fp))
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
            ans, tin, tout, trace, mech = run_once(arm, inst, ch, args.model,
                                                   args.max_rounds, root, schemas)
            rec = {"instance_id": inst["instance_id"], "arm": arm,
                   "model": args.model, "root": root,
                   "tokens_in": tin, "tokens_out": tout,
                   "turns": len(trace) + 1, "tool_trace": trace,
                   "walltime_s": round(time.time() - t0, 1),
                   "mech": mech,
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
        cand = (d.get("mech") or {}).get("candidate_diff") or (d.get("answer") or "")
        prompt = (
            "You are a strict release manager judging whether a candidate patch "
            "correctly fixes the same problem as the reference (gold) patch.\n\n"
            f"[ISSUE]\n{inst['issue'][:3500]}\n\n"
            f"[GOLD PATCH]\n{inst['gold_patch'][:4000]}\n\n"
            f"[CANDIDATE PATCH]\n{cand[:4000]}\n\n"
            "A candidate that is empty, contains tool-call markup instead of a diff, "
            "or has no valid unified diff must be judged fix_equivalent=false "
            "regardless of any prose around it.\n"
            'Reply ONLY with JSON: {"same_issue_area": true/false, '
            '"same_root_cause": true/false, "fix_equivalent": true/false, '
            '"reason": "<=40 words"}')
        votes, errs = [], 0
        for _ in range(max(1, args.judge_votes)):
            try:
                v = AB.parse_verdict((AB.chat(ch, args.model, [
                    {"role": "user", "content": prompt}]) or {})
                    .get("choices", [{}])[0].get("message", {}).get("content", ""))
            except Exception as e:                            # noqa: BLE001
                errs += 1
                d["judge_error"] = str(e)[:120]
                continue
            if isinstance(v, dict):
                votes.append(v)
        if votes:
            def maj(key):
                t = sum(1 for v in votes if v.get(key) is True)
                return t * 2 > len(votes)
            d["judge"] = {"same_issue_area": maj("same_issue_area"),
                          "same_root_cause": maj("same_root_cause"),
                          "fix_equivalent": maj("fix_equivalent"),
                          "reason": votes[-1].get("reason", "")}
            d["judge_votes_n"] = len(votes)
            if not errs:
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
        a = agg.setdefault(d["arm"], {"n": 0, "eq": 0, "root": 0, "appl": 0,
                                      "tin": [], "wall": []})
        a["n"] += 1
        a["eq"] += int(j.get("fix_equivalent") is True)
        a["root"] += int(j.get("same_root_cause") is True)
        a["appl"] += int((d.get("mech") or {}).get("patch_ok") is True)
        for k, bucket in (("tokens_in", "tin"), ("walltime_s", "wall")):
            if d.get(k) is not None:
                a[bucket].append(d[k])
    print(f"{'arm':<4}{'n':>3}{'fix_equiv%':>11}{'same_root%':>11}{'appliable%':>11}"
          f"{'avg_tin':>10}{'avg_wall':>10}")
    for name, s in sorted(agg.items()):
        n = max(s["n"], 1)
        print(f"{name:<4}{s['n']:>3}{s['eq']/n*100:>10.1f}%{s['root']/n*100:>10.1f}%"
              f"{s['appl']/n*100:>10.1f}%"
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
    ap.add_argument("--judge-votes", type=int, default=1,
                    help="判官投票数（>=3 取多数，压单票方差）")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--max-rounds", type=int, default=6)
    ap.add_argument("--redo-ids", default="",
                    help="逗号分隔 instance_id：先删对应结果文件再重跑")
    ap.add_argument("--redo-arms", default="AB", help="redo 限定臂，默认双臂")
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
