# -*- coding: utf-8 -*-
"""重复代码门（S169）：同类代码不许**新增**，只报新增、可收紧基线（与 `god_gate.py` 同形态）。

为什么要有它：上帝对象门管的是"单点过大"，这个门管"多处雷同"——两件不同的事，但都是维护成本的源头。
引擎**复用本仓既有的** `rx-scan sketch`（MinHash bottom-k 指纹，与 MCP 工具 `near_dupes` 同源），
不另造一套：判据 = 文件对的 Jaccard ≥ `--threshold`（默认 0.8，即 near_dupes 的默认口径）。

用法：
  python -X utf8 scripts/dupe_gate.py                 # 判红（对基线）
  python -X utf8 scripts/dupe_gate.py --write-baseline # 记基线（人工过目后提交）
  python -X utf8 scripts/dupe_gate.py --list          # 只列当前重复对
  python -X utf8 scripts/dupe_gate.py --threshold 0.7 --root <dir>

纪律（与 god_gate 一致）：
  · **超基线即红**：出现基线里没有的重复对 ⇒ 退出码 1（`新增即红`）；
  · 低于基线只提示"可收紧"（不红）——基线只准减；
  · 引擎不可用（exe 缺失/超时/非 JSON）⇒ **FAIL 不静默**（不许因为测不了就判绿）；
  · 生成物/夹具面不入册（`bench/manual_snaps`、`bench/results`、`target`、`.git` 等）。
"""
import argparse
import json
import os
import pathlib
import struct
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASELINE = "dupe-baseline.json"
NG, K = 4, 128
EXTS = (".rs", ".py", ".mjs", ".js", ".ts")
EXCLUDE_DIRS = {".git", "target", "node_modules", "__pycache__", "dist", "build",
                ".venv", "venv", "manual_snaps", "results"}
# 测试夹具面天然"长得像"：tests/ 下同族用例、bench 夹具；本门看的是**产品代码**的雷同
EXCLUDE_PREFIX = ("bench/", "tests/", "docs/", "spec/")


def _exe():
    for kind in ("release", "debug"):
        c = pathlib.Path(os.environ.get("TEMP", ".")) / "rx-rs-target" / kind / "rx-scan.exe"
        if c.is_file():
            return str(c)
    env = os.environ.get("UNIFIED_RX_RS_EXE")
    return env if env else None


def collect(root: pathlib.Path):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            if not fn.endswith(EXTS):
                continue
            fp = pathlib.Path(dirpath) / fn
            rel = fp.relative_to(root).as_posix()
            if rel.startswith(EXCLUDE_PREFIX):
                continue
            out.append((rel, str(fp)))
    return sorted(out)


def sketch(exe: str, paths):
    buf = b"".join(struct.pack("<I", len(p.encode("utf-8"))) + p.encode("utf-8")
                   for p in paths) + struct.pack("<I", 0)
    try:
        cp = subprocess.run([exe, "sketch", str(NG), str(K)], capture_output=True,
                            timeout=180, input=buf)
    except subprocess.TimeoutExpired:
        return None, "rx-scan sketch 超时（180s）"
    lines = (cp.stdout or b"").decode("utf-8", errors="replace").strip().splitlines()
    if not lines:
        return None, f"无输出（exit={cp.returncode}）"
    try:
        out = json.loads(lines[-1])
    except ValueError:
        return None, f"输出非 JSON: {lines[-1][:120]}"
    return out.get("files", []), None


def jaccard(a, b):
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / len(a | b)


def pairs_above(files, thr):
    """返回 {("a","b"): 相似度}（a<b 规范化）。指纹=bottom-k ⇒ 集合交并即 MinHash 估计。"""
    fps = [(f["path"], set(int(h) for h in f.get("fingerprint", []))) for f in files]
    fps = [(p, s) for p, s in fps if len(s) >= 16]        # 太小的样本不判（噪声）
    out = {}
    for i in range(len(fps)):
        for j in range(i + 1, len(fps)):
            sim = jaccard(fps[i][1], fps[j][1])
            if sim >= thr:
                a, b = sorted((fps[i][0], fps[j][0]))
                out[f"{a}|{b}"] = round(sim, 3)
    return out


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--threshold", type=float, default=0.8)
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--max-files", type=int, default=800)
    a = ap.parse_args()
    root = pathlib.Path(a.root).resolve()
    bpath = root / BASELINE
    exe = _exe()
    if not exe:
        print("DUPE-GATE FAIL 引擎不可用：rx-scan.exe 不在 %TEMP%/rx-rs-target/{release,debug}"
              "（或设 UNIFIED_RX_RS_EXE）——不静默判绿")
        return 2
    files = collect(root)
    if len(files) > a.max_files:
        files = files[:a.max_files]
    shrunk = sketch(exe, [p for _, p in files])
    if shrunk[0] is None:
        print(f"DUPE-GATE FAIL 引擎调用失败：{shrunk[1]}——不静默判绿")
        return 2
    cur = pairs_above(shrunk[0], a.threshold)
    print(f"DUPE-GATE root={root} 文件={len(files)} 阈值={a.threshold}（ng={NG} k={K}）当前重复对={len(cur)}")
    for key, sim in sorted(cur.items(), key=lambda kv: -kv[1])[:12]:
        print(f"  {sim:.3f}  {key}")
    if len(cur) > 12:
        print(f"  …另有 {len(cur) - 12} 对")
    if a.list:
        return 0
    if a.write_baseline:
        payload = json.dumps({"policy": "重复对基线：新增即红（scripts/dupe_gate.py）；"
                                        "只登记产品代码面（bench/tests/docs/spec 不入册）",
                              "threshold": a.threshold, "pairs": sorted(cur)},
                             ensure_ascii=False, indent=1) + "\n"
        tmp = bpath.with_suffix(bpath.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, bpath)                     # 原子替换（同 god_gate：中断不留半截）
        print(f"已写基线 {bpath.name}（{len(cur)} 对）——此后只准减")
        return 0
    if not bpath.is_file():
        print(f"警告：无 {bpath.name} ⇒ 先跑 --write-baseline 才会管住存量")
        return 0
    base = set(json.loads(bpath.read_text(encoding="utf-8"))["pairs"])
    new = sorted(set(cur) - base)
    gone = sorted(base - set(cur))
    for k in new:
        print(f"  ✗ 新增重复对 {cur[k]:.3f}  {k}")
    if gone:
        print(f"  （{len(gone)} 对已消失，可收紧基线）")
    if new:
        print(f"DUPE-GATE FAIL 新增={len(new)}")
        return 1
    print("DUPE-GATE OK 无新增重复对")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
