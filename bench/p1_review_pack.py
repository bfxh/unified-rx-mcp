# -*- coding: utf-8 -*-
"""p1_review_pack.py —— 人工标注评审包：候选行（评审者自己的超集 grep）± 上下文。"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SNAPS = os.path.join(HERE, "manual_snaps")
OUT = os.path.join(HERE, "manual_review_pack.txt")

# 评审者超集（比 bug_scan 的正则更宽：字符串/调用形式都抓）
CAND = [
    re.compile(r"\.unwrap\s*\("),
    re.compile(r"\.expect\s*\("),
    re.compile(r"\bpanic!\s*\("),
    re.compile(r"\bunreachable!\s*\("),
    re.compile(r"\b(todo|unimplemented)!\s*\("),
    re.compile(r"\bas\s+(i64|i32|u64|u32|f64|f32|usize|isize|u8|i8|i16|u16)\b"),
    re.compile(r"^\s*except\s*:"),
    re.compile(r"\beval\s*\("),
    re.compile(r"\bexec\s*\("),
    re.compile(r"==\s*0\.\d"),          # 浮点字面量相等比较
    re.compile(r"0\.\d\s*=="),
    re.compile(r"\.single\s*\("),        # bevy query single
]
CTX = 10


def main():
    out = []
    for name in sorted(os.listdir(SNAPS)):
        src = open(os.path.join(SNAPS, name), encoding="utf-8",
                   errors="replace").read().split("\n")
        hits = []
        for i, line in enumerate(src):
            if any(c.search(line) for c in CAND):
                hits.append(i)
        if not hits:
            continue
        out.append(f"\n{'='*20} {name} 候选 {len(hits)} 处 {'='*20}")
        shown = set()
        for h in hits:
            lo, hi = max(0, h - CTX), min(len(src), h + CTX + 1)
            if h in shown:
                out.append(f"--- L{h+1} (已展示过)")
                continue
            out.append(f"--- L{h+1} ---")
            for j in range(lo, hi):
                mark = ">>" if j == h else "  "
                out.append(f"{mark}{j+1:4d}| {src[j]}")
                shown.add(j)
    with open(OUT, "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(out))
    print(f"[OK] {len(out)} 行评审包 -> {os.path.relpath(OUT, ROOT)}")


if __name__ == "__main__":
    main()
