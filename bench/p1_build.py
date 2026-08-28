# -*- coding: utf-8 -*-
"""p1_build.py —— P1 语料挖掘：VoxelForge 系 git 历史 → 标注 bug 库。

条目（bench/bug_corpus.jsonl）：
  {"id","repo","fix_sha","parent_sha","file","rule_expect","sample","evidence"}
  bug 态 = parent_sha 的文件内容（bug_scan 命中）；clean 态 = fix_sha 内容（应清零）。
挖掘法：逐文件历史相邻版本各扫一遍，某规则计数下降 → 该版本对即 bug→fix 对，
每 (文件,规则) 只留最近一次下降。这样条目保证"bug_scan 原则上可检出"，P/R 才有测量意义。
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import registry  # noqa: E402
import tools  # noqa: E402,F401

OUT = os.path.join(HERE, "bug_corpus.jsonl")
REPOS = ["VoxelForge", "VoxelForge-V3"]
FILE_RE = re.compile(r"\.(py|rs)$")
DEV = r"D:\开发"
DEPTH = 120


def git(repo, *args):
    return subprocess.run(["git", "-C", os.path.join(DEV, repo)] + list(args),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120).stdout


def scan_text(src, suffix):
    d = tempfile.mkdtemp(prefix="p1scan")
    fp = os.path.join(d, "f" + suffix)
    with open(fp, "w", encoding="utf-8", newline="") as f:
        f.write(src)
    r = registry.call("bug_scan", {"path": fp})
    return (r.get("result", {}).get("issues") or []), fp, d


def file_at(repo, sha, path):
    out = subprocess.run(["git", "-C", os.path.join(DEV, repo), "show",
                          f"{sha}:{path}"],
                         capture_output=True, timeout=120)
    return out.stdout.decode("utf-8", errors="replace") if out.returncode == 0 else None


def mine():
    """逐文件历史：相邻版本各扫一遍，家族计数下降 → 该版本对即 bug→fix 对。"""
    entries, seen = [], set()
    for repo in REPOS:
        files = (git(repo, "ls-files", "*.py", "*.rs") or "").splitlines()
        for path in files:
            path = path.strip()
            if not FILE_RE.search(path):
                continue
            hist = [l.strip() for l in (git(repo, "log", "--pretty=%H", "--",
                                            path) or "").splitlines() if l.strip()]
            for child, parent in zip(hist, hist[1:]):
                psrc = file_at(repo, parent, path)
                csrc = file_at(repo, child, path)
                if psrc is None or csrc is None or len(psrc) > 400_000:
                    continue
                pissues, _, _ = scan_text(psrc, os.path.splitext(path)[1])
                if not pissues:
                    continue
                cissues, _, _ = scan_text(csrc, os.path.splitext(path)[1])
                pfam = Counter(x["rule"] for x in pissues)
                cfam = Counter(x["rule"] for x in cissues)
                for rule, n in pfam.items():
                    if cfam.get(rule, 0) >= n:
                        continue                  # 未下降
                    k = (repo, path, rule)        # 每 (文件,规则) 只留最近一次下降
                    if k in seen:
                        continue
                    seen.add(k)
                    hit = next(x for x in pissues if x["rule"] == rule)
                    entries.append({
                        "repo": repo, "fix_sha": child, "parent_sha": parent,
                        "file": path, "rule_expect": rule,
                        "evidence": f"{hit['msg']} @L{hit['line']} (count {n}->{cfam.get(rule, 0)})",
                        "subject": f"{path} {rule} {n}->{cfam.get(rule, 0)}",
                    })
    return entries


def main():
    bugs = mine()
    by_fam = {}
    for b in bugs:
        by_fam.setdefault(b["rule_expect"], []).append(b)
    final, fams, idx = [], list(by_fam), 0
    while len(final) < 22 and any(by_fam[f] for f in fams):
        f = fams[idx % len(fams)]
        idx += 1
        if by_fam[f]:
            final.append(by_fam[f].pop(0))
    for i, b in enumerate(final):
        b["id"] = f"VB-{i+1:02d}"
        b["sample"] = "bug"
    # clean 样本：近期提交里 bug_scan 零命中的文件（负类，P 的分母）
    extra, n_clean = [], 0
    clean_target = max(8, 30 - len(final))
    for repo in REPOS:
        shas = [l.split(" ", 1)[0] for l in
                (git(repo, "log", "--all", "--pretty=%H") or "").splitlines()[:120]]
        for sha in shas:
            if n_clean >= clean_target:
                break
            files = re.findall(r"^(\S+\.(?:py|rs))$",
                               git(repo, "show", "--name-only", "--pretty=format:",
                                   sha) or "", re.M)
            for path in files[:1]:
                if any(e["repo"] == repo and e["file"] == path for e in extra):
                    continue
                src = file_at(repo, sha, path)
                if src is None or len(src) > 200_000:
                    continue
                issues, tmpfp, tmpd = scan_text(src, os.path.splitext(path)[1])
                try:
                    os.remove(tmpfp)
                    os.rmdir(tmpd)
                except OSError:
                    pass
                if issues:
                    continue
                extra.append({"id": f"VB-C{n_clean+1:02d}", "repo": repo,
                              "fix_sha": sha, "parent_sha": sha, "file": path,
                              "rule_expect": None, "sample": "clean",
                              "evidence": "", "subject": "clean state"})
                n_clean += 1
    with open(OUT, "w", encoding="utf-8") as f:
        for c in final + extra:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"[OK] bug {len(final)} + clean {n_clean} = {len(final)+n_clean} -> "
          f"{os.path.relpath(OUT, ROOT)}")
    print(Counter(b["rule_expect"] for b in final))


if __name__ == "__main__":
    main()
