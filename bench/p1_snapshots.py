# -*- coding: utf-8 -*-
"""p1_snapshots.py —— 人工标注快照抽取（选择独立于 bug_scan 输出）。

15 个语料文件 × 2 版本（HEAD + seed 随机历史版）= 30 快照。
快照落盘 bench/manual_snaps/<id>__<file> 供人工阅读标注。
"""
import json
import os
import random
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEV = r"D:\开发"
SNAP_DIR = os.path.join(HERE, "manual_snaps")
OUT = os.path.join(HERE, "manual_snapshots.jsonl")
SEED = 20260828


def git(repo, *args):
    return subprocess.run(["git", "-C", os.path.join(DEV, repo)] + list(args),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120).stdout


def main():
    os.makedirs(SNAP_DIR, exist_ok=True)
    random.seed(SEED)
    corpus = [json.loads(l) for l in open(os.path.join(HERE, "bug_corpus.jsonl"),
                                          encoding="utf-8") if l.strip()]
    files = sorted({(e["repo"], e["file"]) for e in corpus})
    snaps = []
    for repo, path in files:
        hist = [l.strip() for l in (git(repo, "log", "--pretty=%H", "--",
                                        path) or "").splitlines() if l.strip()]
        if not hist:
            continue
        picks = {"head": hist[0]}
        if len(hist) > 3:
            picks["rand"] = hist[random.randrange(1, len(hist))]
        for tag, sha in picks.items():
            out = subprocess.run(["git", "-C", os.path.join(DEV, repo), "show",
                                  f"{sha}:{path}"], capture_output=True, timeout=60)
            if out.returncode != 0:
                continue
            src = out.stdout.decode("utf-8", errors="replace")
            sid = f"{repo.split('-')[-1]}_{len(snaps)+1:02d}_{tag}"
            snapname = f"{sid}__{os.path.basename(path)}"
            with open(os.path.join(SNAP_DIR, snapname), "w",
                      encoding="utf-8", newline="") as f:
                f.write(src)
            snaps.append({"snap_id": sid, "repo": repo, "sha": sha, "file": path,
                          "snap_file": snapname, "lines": src.count("\n") + 1,
                          "bytes": len(src)})
    with open(OUT, "w", encoding="utf-8") as f:
        for s in snaps:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    big = sum(1 for s in snaps if s["lines"] > 400)
    print(f"[OK] {len(snaps)} snapshots -> {os.path.relpath(SNAP_DIR, ROOT)} "
          f"(>400 行的 {big} 个)")
    for s in snaps:
        print(f"  {s['snap_id']:<16} {s['lines']:>5}L  {s['file']}")


if __name__ == "__main__":
    main()
