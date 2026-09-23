# -*- coding: utf-8 -*-
"""上帝对象门：**改基线前先看差**（`--write-baseline` 会一次性重记所有值，等于放松棘轮）。

用法：
  python -X utf8 scripts/god_diff.py              # 只列变动的文件（默认基线 god-baseline.json）
  python -X utf8 scripts/god_diff.py --all        # 连没变的一起列

为什么要有它（S169 实测）：掩码修好后（生命周期不再被当字面量），旧基线里一大批 Rust 文件的
`max_fn_lines` 是**伪小值**（真实 307 行被记成 63 行）。直接 `--write-baseline` 会把"纠正测量"
和"真变胖"混在一起记下去——先看差才能逐条判断哪条是纠正、哪条是该拦的。
"""
import argparse
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
KEYS = ("file_lines", "max_fn_lines", "max_type_members")


def load_gate():
    spec = importlib.util.spec_from_file_location("god_gate_for_diff",
                                                  ROOT / "scripts" / "god_gate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    gg = load_gate()
    root = pathlib.Path(a.root).resolve()
    cfg = gg.load_cfg(root, None)
    files = gg.scan(root, cfg)
    base = gg.load_baseline(root / cfg["baseline"])
    rows = []
    for rel, m in files.items():
        b = base.get(rel)
        if b is None:
            rows.append((rel, "新增文件", {k: (None, m[k]) for k in KEYS}))
            continue
        d = {k: (b.get(k, 0), m[k]) for k in KEYS if b.get(k, 0) != m[k]}
        if d:
            rows.append((rel, "", d))
    rows.sort(key=lambda r: -max(abs((v[1] or 0) - (v[0] or 0)) for v in r[2].values()))
    for rel, note, d in rows:
        parts = []
        for k, (o, n) in d.items():
            arrow = "↑" if (o is not None and n > o) else ("↓" if o is not None else "+")
            parts.append(f"{k} {o}→{n}{arrow}")
        print(f"  {rel}{note}　" + "　".join(parts))
    print(f"共 {len(rows)} 个条目有差（扫描 {len(files)} 个文件，基线 {len(base)} 条）")
    up = [r for r in rows if any(o is not None and n > o for o, n in r[2].values())]
    print(f"其中含「变大」的 {len(up)} 条 —— 逐条判断是「纠正测量」还是「真变胖」")
    return 0


if __name__ == "__main__":
    sys.exit(main())
