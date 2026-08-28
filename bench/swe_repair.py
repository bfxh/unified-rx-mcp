# -*- coding: utf-8 -*-
"""swe_repair.py —— S25 真·闭环：测试执行失败输出回喂模型做修复轮。

与 S24 的区别：S24 只"判分"，本模块把执行结果喂回模型让它改自己的补丁，
直到 fail-to-pass 真转绿或轮次耗尽。

用法：
  python bench/swe_repair.py --run [--max-repairs 3]
  python bench/swe_repair.py --summary

对每个 feasible（有 venv）任务的 A/B 结果文件：
  round0 = 已有 candidate_diff（S23 产物）；没有则走"定位+文件内容注入"产出 sr 块
  loop   = apply → 跑 FTB → 失败则把【测试失败输出 + 触碰文件当前内容】喂回模型
           换取修正的 sr 块 → 再跑；至多 --max-repairs 轮
  终态 FTB 全 PASS 且 PTB 抽样不破 → repair.verified = True
结果写回 result 文件的 "repair" 字段，幂等可重入。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import ab_run as AB
import swe_p3
import swe_verify as sv
import registry

MAX_FILES = 3
FILE_CAP = 9000
FTB_TAIL_CAP = 2500


def log(msg):
    print(msg, flush=True)


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _chat(ch, model, msgs):
    try:
        return AB.chat(ch, model, msgs)
    except SystemExit as e:
        return {"choices": [{"message": {"content": f"(API FAIL) {e}"}}]}


def _last_content(resp):
    return ((resp.get("choices") or [{}])[0].get("message", {}).get("content") or "")


def _touched_files(diff):
    return sorted({m.group(1) for m in
                   re.finditer(r"^diff --git a/(\S+) b/", diff, re.M)})[:MAX_FILES]


def _locate_ok(root, path):
    """S29：locate 轮的路径存在性检查必须锁在 root 内。"""
    fp = swe_p3.safe_join(root, path)
    return bool(fp) and os.path.isfile(fp)


def _file_block(root, path):
    fp = swe_p3.safe_join(root, path)
    if fp is None:
        return ""
    try:
        with open(fp, encoding="utf-8", errors="replace") as f:
            c = f.read().replace("\r\n", "\n")
    except OSError:
        return ""
    if len(c) > FILE_CAP:
        c = c[:FILE_CAP // 2] + "\n…[middle truncated]…\n" + c[-FILE_CAP // 2:]
    return f"===== ACTUAL CONTENT of {path} =====\n{c}"


def _fresh_issue_block(inst):
    return (f"[SWE issue · {inst['instance_id']}]\n"
            f"{inst['issue'][:3500]}\n\n"
            "Produce the fix as search/replace blocks (```sr with path:/"
            "<<<<<<< SEARCH/=======/>>>>>>> REPLACE). First list the file(s) you "
            "need to see, one per line as `path: <relpath>`, and I will show them.")


def _locate_and_ground(inst, root, ch, model, msgs):
    """定位轮 + 文件内容注入（S23 接地定稿的复用）。返回模型回复文本。"""
    loc = _last_content(_chat(ch, model, msgs))
    if swe_p3.parse_dsml(loc):
        msgs.append({"role": "assistant", "content": loc})
        outs = []
        for fn, fa in swe_p3.parse_dsml(loc):
            txt, _ = AB.exec_tool(fn, fa)
            outs.append(f"$ {fn}\n{txt}")
        msgs.append({"role": "user", "content": "[tool results]\n" +
                     "\n\n".join(outs)[:20000] +
                     "\n\nNow list the file path(s) as `path: <relpath>`."})
        loc = _last_content(_chat(ch, model, msgs))
    paths = []
    for pm in re.finditer(r"^\s*path:\s*(\S+)\s*$", loc or "", re.M):
        p = pm.group(1).replace("\\", "/").lstrip("/").strip(".,`;*\"'()[]")
        if (p not in paths and len(paths) < MAX_FILES and _locate_ok(root, p)):
            paths.append(p)
    parts = [b for p in paths for b in [_file_block(root, p)] if b]
    msgs.append({"role": "assistant", "content": loc})
    if parts:
        msgs.append({"role": "user", "content": "\n\n".join(parts)[:26000] +
                     "\n\nOutput ONLY the final search/replace edit blocks, in "
                     "EXACTLY this format:\n" + SR_TEMPLATE +
                     "\nEvery block MUST start with a `path:` line. COPY SEARCH "
                     "lines verbatim from these contents."})
    else:
        msgs.append({"role": "user", "content":
                     "Output ONLY the final search/replace edit blocks (```sr) in "
                     "the path:/SEARCH/=======/REPLACE format."})
    return _last_content(_chat(ch, model, msgs))


def _apply_and_diff(root, blocks):
    """sr 块 → 应用 + git diff（apply_sr 还原后再打回，保证可测试态）。"""
    applied, fails, gdiff, fz, grounds = swe_p3.apply_sr(root, blocks)
    if gdiff.strip():
        r = subprocess_apply(root, gdiff)
        if r != 0:
            return 0, fails, "", grounds
    return applied, fails, gdiff, grounds


def subprocess_apply(root, diff):
    import subprocess
    r = subprocess.run(["git", "-C", root, "apply", "--whitespace=nowarn", "-"],
                       input=diff.replace("\r\n", "\n").encode(),
                       capture_output=True, timeout=120)
    return r.returncode


SR_TEMPLATE = ("```sr\npath: <relpath>\n<<<<<<< SEARCH\n<exact lines copied from "
               "the file>\n=======\n<replacement lines>\n>>>>>>> REPLACE\n```")


def repair_loop(args):
    ch = AB.load_channel(args.channel)
    model = args.model
    insts = {d["instance_id"]: d for d in swe_p3.load_sample()}
    import glob
    files = sorted(glob.glob(os.path.join(sv.RESULTS_DIR, "*_A.json")) +
                   glob.glob(os.path.join(sv.RESULTS_DIR, "*_B.json")))
    if args.ids:
        want = {x.strip() for x in args.ids.split(",")}
        files = [f for f in files if json.load(open(f, encoding="utf-8"))
                 ["instance_id"] in want]
    base_cache = {}
    done = 0
    for fp in files:
        rec = json.load(open(fp, encoding="utf-8"))
        if "repair" in rec and not args.force:
            continue
        iid = rec["instance_id"]
        inst = insts.get(iid)
        if inst is None or inst["repo"] not in sv.PY_REPOS:
            continue
        if not (inst.get("test_patch") and inst.get("ftb")):
            continue
        py = sv._uv_py(os.path.join(sv.ENVS, iid.replace("/", "__")))
        if not os.path.exists(py):
            continue
        root = os.path.join(sv.WORK, iid.replace("/", "__"))
        if not os.path.isdir(root):
            continue

        t0 = time.time()
        sv._restore(root)
        r = subprocess.run(["git", "-C", root, "apply", "--whitespace=nowarn", "-"],
                           input=inst["test_patch"].replace("\r\n", "\n").encode(),
                           capture_output=True, timeout=120)
        if r.returncode != 0:
            rec["repair"] = {"skip": "test-patch-apply-failed"}
            json.dump(rec, open(fp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            continue
        ftb = list(inst.get("ftb") or [])
        bkey = iid + "::base"
        if bkey not in base_cache:
            rc, _ = sv._run_tests(inst, py, root, ftb)
            base_cache[bkey] = (rc is not None and rc != 0)
        if not base_cache[bkey]:
            rec["repair"] = {"skip": "base-already-green"}
            json.dump(rec, open(fp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            continue

        cand = (rec.get("mech") or {}).get("candidate_diff") or ""
        rounds = []
        diff = cand
        cur = diff
        if cur.strip():
            rc = subprocess_apply(root, cur)
            rounds.append({"src": "s23", "applied": rc == 0})
            if rc != 0:
                cur = ""
        else:
            rounds.append({"src": "none"})
        if not cur.strip():
            # 全新起点：定位 + 接地
            msgs = [{"role": "system", "content":
                     "You are a senior engineer fixing a real repository issue. "
                     f"Repo root: {root}"},
                    {"role": "user", "content": _fresh_issue_block(inst)}]
            ans = _locate_and_ground(inst, root, ch, model, msgs)
            blocks = swe_p3.parse_sr(ans)
            applied, fails, gdiff, _ = _apply_and_diff(root, blocks)
            rounds.append({"src": "fresh", "applied": applied, "fails": fails[:5]})
            cur = gdiff

        verified = False
        for rnd in range(args.max_repairs + 1):
            if cur.strip():
                if not _applied_now(root, cur):
                    subprocess_apply(root, cur)
            rc, tail = sv._run_tests(inst, py, root, ftb)
            ftb_pass = (rc == 0)
            rounds.append({"round": rnd, "ftb_pass": ftb_pass,
                           "tail": (tail or "")[-600:] if not ftb_pass else ""})
            if ftb_pass:
                verified = True
                break
            if rnd == args.max_repairs:
                break
            # 回喂：失败输出 + 结构化帧（S33：ide 解析器）+ LSP 诊断（S35）+ 触碰文件当前内容
            files_show = _touched_files(cur) if cur.strip() else []
            msgs = [{"role": "system", "content":
                     "You are a senior engineer. Your patch did NOT make the "
                     "failing tests pass. Fix it."},
                    {"role": "user", "content":
                     f"[SWE issue · {iid}]\n{inst['issue'][:2500]}"}]
            if cur.strip():
                msgs.append({"role": "user", "content":
                             "[YOUR PREVIOUS PATCH]\n" + cur[:3500]})
            frames_txt = _structured_frames(tail or "")
            lsp_txt = _diag_section(root, files_show)
            # S37：断点命中——补丁行的实际运行时 locals（pytest 在 settrace 下执行）
            bps_txt = ""
            if cur.strip() and py:
                hits = _break_hits(root, py, _changed_lines(cur), list(ftb)[:6])
                bps_txt = _break_section(hits)
            msgs.append({"role": "user", "content":
                         "[TEST FAILURE OUTPUT]\n" + (tail or "")[:FTB_TAIL_CAP] +
                         (("\n\n" + frames_txt[:2500]) if frames_txt else "") +
                         (("\n\n" + lsp_txt[:1500]) if lsp_txt else "") +
                         (("\n\n" + bps_txt[:1500]) if bps_txt else "") +
                         "\n\n[CURRENT FILE CONTENTS]\n" +
                         "\n\n".join(b for p in files_show
                                     for b in [_file_block(root, p)] if b)[:22000] +
                         "\n\nOutput ONLY corrected search/replace blocks, one per "
                         "edit, in EXACTLY this format:\n" + SR_TEMPLATE +
                         "\nEvery block MUST start with a `path:` line (one of: " +
                         ", ".join(files_show[:MAX_FILES] or ["<relpath>"]) +
                         "). COPY SEARCH lines verbatim from the current contents."})
            ans = _last_content(_chat(ch, model, msgs))
            if swe_p3.parse_dsml(ans):
                outs = []
                for fn, fa in swe_p3.parse_dsml(ans):
                    txt, _ = AB.exec_tool(fn, fa)
                    outs.append(f"$ {fn}\n{txt}")
                msgs.append({"role": "assistant", "content": ans})
                msgs.append({"role": "user", "content": "[tool results]\n" +
                             "\n\n".join(outs)[:20000] +
                             "\n\nNow output ONLY corrected ```sr blocks."})
                ans = _last_content(_chat(ch, model, msgs))
            blocks = swe_p3.parse_sr(ans)
            applied, fails, gdiff, _ = _apply_and_diff(root, blocks)
            rounds.append({"round": rnd, "repair_applied": applied,
                           "repair_fails": fails[:5]})
            cur = gdiff

        ptb = {}
        if verified:
            ptb_list = (inst.get("ptb") or [])[:sv.PTB_CAP]
            if ptb_list:
                rc2, _ = sv._run_tests(inst, py, root, ptb_list)
                ptb = {"ptb_total": len(ptb_list), "ptb_pass": rc2 == 0}
            if ptb and not ptb["ptb_pass"]:
                verified = False
        sv._restore(root)
        rec["repair"] = {"verified": verified, "rounds_used": len(rounds),
                         "rounds": rounds[:12], **ptb,
                         "walltime_s": round(time.time() - t0, 1)}
        json.dump(rec, open(fp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        log(f"repair {os.path.basename(fp)} -> verified={verified} "
            f"rounds={len(rounds)} ({rec['repair']['walltime_s']}s)")
        done += 1
    log(f"[OK] repair done {done} files")


def _lsp_diagnostics(root, files):
    """S35：补丁触碰文件的 LSP 诊断（publishDiagnostics 拉取）。
    pylsp 冷启动会话在 lsp.py 内缓存；失败/无诊断返回空——诚实不硬造。"""
    out = []
    for p in files[:2]:
        fp = os.path.join(root, p.replace("/", os.sep))
        if not os.path.isfile(fp):
            continue
        try:
            r = registry.call("ide_lsp", {"action": "diagnostics", "file": fp})
            res = r.get("result") or {}
            dias = res.get("diagnostics") or []
            for d in dias:
                if d.get("severity") == "error" or d.get("severity") == 1:
                    out.append({"file": p, "line": d.get("line"),
                                "msg": (d.get("msg") or "")[:160]})
        except Exception:
            return []            # LSP 不可用 → 如实放弃该信号
    return out[:10]


def _lsp_section(dias):
    if not dias:
        return ""
    lines = ["[LSP DIAGNOSTICS · patch 引入的错误]"]
    for d in dias:
        lines.append(f"- {d['file']}:{d.get('line', '?')} {d['msg']}")
    return "\n".join(lines)


def _structured_frames(text):
    """S33/S34：把测试失败输出解析成结构化帧文本段（无帧返回空）。"""
    from tools.ide import (_parse_py_traceback, _parse_java_trace,
                           _parse_go_panic, _parse_pytest)
    parts = []
    pf, asserts = _parse_pytest(text)
    if pf or asserts:
        lines = ["[STRUCTURED · pytest]"]
        for t in pf[:10]:
            lines.append(f"- FAILED {t}")
        for a in asserts[:5]:
            lines.append(f"  E {a}")
        parts.append("\n".join(lines))
    frames, last = _parse_py_traceback(text)
    if frames:
        lines = ["[STRUCTURED FRAMES · python]"]
        for f in frames[:8]:
            lines.append(f"- {f['file']}:{f['line']} in {f['fn']}")
        if last:
            lines.append(f"  last: {last}")
        parts.append("\n".join(lines))
    jf, jl = _parse_java_trace(text)
    if jf:
        lines = ["[STRUCTURED FRAMES · java]"]
        for f in jf[:8]:
            lines.append(f"- {f['cls']} ({f['file']}:{f['line']})")
        if jl:
            lines.append(f"  last: {jl}")
        parts.append("\n".join(lines))
    gp = _parse_go_panic(text)
    if gp:
        lines = ["[STRUCTURED FRAMES · go]"]
        for p in gp[:3]:
            lines.append(f"- panic: {p['msg']}")
            for b in p["backtrace"][:5]:
                lines.append(f"  at {b['file']}:{b['line']}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _changed_lines(diff):
    """候选 diff → {path: [(start, end)]}（新文件行号区间，S37 断点回喂用）。"""
    out = {}
    cur = None
    for line in (diff or "").splitlines():
        m = re.match(r"^diff --git a/(\S+) b/", line)
        if m:
            cur = m.group(1)
            continue
        m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if m and cur:
            start = int(m.group(1))
            n = int(m.group(2) or 1)
            if n:
                out.setdefault(cur, []).append((start, start + n - 1))
    return out


def _break_hits(root, py, changed, test_ids, max_hits=12):
    """S37：候选补丁行上跑断点记录器（pytest 在 settrace 下执行），
    抓补丁行的实际运行时 locals——模型据此判断补丁算出的值对不对。"""
    bps = []
    for path, ranges in changed.items():
        for a, _b in ranges[:4]:
            bps.append({"file": path, "line": a})
    if not bps or not test_ids:
        return []
    try:
        r = registry.call("ide_break", {"path": root,
                                        "cmd": [py, "-m", "pytest",
                                                *test_ids],
                                        "breakpoints": bps[:8],
                                        "max_hits": max_hits})
        res = r.get("result") or {}
        return res.get("hits") or []
    except Exception:
        return []                    # 断点后端不可用 → 如实放弃该信号


def _break_section(hits):
    if not hits:
        return ""
    lines = ["[BREAKPOINT HITS · 补丁行运行时状态]"]
    for h in hits[:6]:
        locs = "; ".join(f"{k}={v}" for k, v in
                         list(h.get("locals", {}).items())[:6])
        lines.append(f"- line {h.get('bp_line')}: {locs}")
        st = (h.get("stack") or [{}])[0]
        if st.get("fn"):
            lines.append(f"  in {st['fn']} ({st.get('file')}:{st.get('line')})")
    return "\n".join(lines)


def _diag_section(root, files):
    """S37：统一诊断通道（LSP + clippy），只回喂 error 级。"""
    try:
        r = registry.call("ide_diagnostics", {"path": root, "files": files,
                                              "include_lint": True})
        res = r.get("result") or r
        dias = res.get("diagnostics") or []
        errs = [d for d in dias if d["severity"] == "error"]
        if not errs:
            return ""
        lines = ["[DIAGNOSTICS · patch 引入的静态错误]"]
        for d in errs[:8]:
            lines.append(f"- [{d['source']}] {d['file']}:{d['line']} "
                         f"{d['message']}")
        return "\n".join(lines)
    except Exception:
        return ""


def _applied_now(root, diff):
    """粗查：diff 是否已在工作树（避免重复 apply 失败）。"""
    import subprocess
    r = subprocess.run(["git", "-C", root, "apply", "--check", "-"],
                       input=diff.replace("\r\n", "\n").encode(),
                       capture_output=True, timeout=120)
    return r.returncode != 0          # check 失败 = 已应用（上下文对不上）


def summary():
    import glob
    agg = {}
    for fp in sorted(glob.glob(os.path.join(sv.RESULTS_DIR, "*_*.json"))):
        if os.path.basename(fp) == "summary.json":
            continue
        d = json.load(open(fp, encoding="utf-8"))
        v = d.get("verify") or {}
        r = d.get("repair") or {}
        a = agg.setdefault(d["arm"], {"n": 0, "feasible": 0, "verified_s24": 0,
                                      "looped": 0, "verified_s25": 0})
        a["n"] += 1
        if "skip" not in v:
            a["feasible"] += 1
            a["verified_s24"] += int(v.get("verified") is True)
        if "skip" not in r and r:
            a["looped"] += 1
            a["verified_s25"] += int(r.get("verified") is True)
    print(f"{'arm':<4}{'n':>4}{'feasible':>10}{'s24_ok':>8}{'looped':>8}"
          f"{'s25_ok':>8}{'lift':>8}")
    for name, s in sorted(agg.items()):
        print(f"{name:<4}{s['n']:>4}{s['feasible']:>10}{s['verified_s24']:>8}"
              f"{s['looped']:>8}{s['verified_s25']:>8}"
              f"{s['verified_s25'] - s['verified_s24']:>8}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--ids", default="")
    ap.add_argument("--max-repairs", type=int, default=3)
    ap.add_argument("--channel", default="conn-deepseek")
    ap.add_argument("--model", default="deepseek-chat")
    a = ap.parse_args()
    if a.run:
        repair_loop(a)
    if a.summary:
        summary()
    if not (a.run or a.summary):
        print(__doc__)


if __name__ == "__main__":
    main()
