# -*- coding: utf-8 -*-
"""CI 数据流门（S139）：dogfood `rust_taint_scan`——产品面 definite 发现对照基线。

范围口径：
- 扫描面 = 全仓 **除 bench/**（bench=开发夹具面，如实排除并在输出注明）；
- 门 = 产品面 definite（kind=definite）计数**不得超出** `spec/taint-baseline.json`
  基线（按 (file, sink) 计数比对；行号漂移不误伤）；新增即红，需人工确认后
  `--update-baseline` 显式入册。
- 与 Secrets gate / Self-attack gate 同纪律：沙盒自给自足声明。

用法：
  python scripts/taint_gate.py                     # 门判定（CI 同款）
  python scripts/taint_gate.py --update-baseline   # 按当前现实重写基线（人工过目后提交）
"""
import json
import os
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("UNIFIED_RX_SANDBOX", ROOT)

import registry  # noqa: E402
import tools  # noqa: E402,F401

BASELINE = Path(ROOT) / "spec" / "taint-baseline.json"   # pathlib 字面量组件
EXCLUDE_PREFIX = "bench/"


def norm(p):
    return str(p).replace("\\", "/")


def current_counts():
    r = registry.call("rust_taint_scan", {"root": ROOT})
    if not r.get("ok"):
        sys.exit(f"rust_taint_scan 调用失败: {r.get('error')}")
    res = r["result"]
    counts = {}
    total_def = 0
    for f in res.get("findings") or []:
        if f.get("kind") != "definite":
            continue
        fp = norm(f["file"])
        if fp.startswith(EXCLUDE_PREFIX):
            continue
        total_def += 1
        key = f"{fp}\t{f['sink']}"
        counts[key] = counts.get(key, 0) + 1
    return counts, total_def, res


def load_baseline():
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def save_baseline(counts):
    entries = []
    for key in sorted(counts):
        fp, sink = key.split("\t")
        entries.append({"file": fp, "sink": sink, "count": counts[key],
                        "why": "<请填写：为何属良性/设计内>"})
    doc = {
        "policy": ("产品面 definite 基线：新增即红（scripts/taint_gate.py）；"
                   "bench/ 为开发夹具面不在册；为何字段=人工确认理由"),
        "entries": entries,
    }
    BASELINE.write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                        encoding="utf-8", newline="")
    print(f"TAINT-BASELINE updated: {len(entries)} entries -> {BASELINE}")


def main(argv):
    counts, total_def, res = current_counts()
    print(f"扫描完成：findings={len(res.get('findings') or [])} "
          f"产品面 definite={total_def}（bench/ 已排除）"
          f" cross_file={res.get('cross_file_findings')}")
    if "--update-baseline" in argv:
        save_baseline(counts)
        return
    base = load_baseline()
    allowed = {(e["file"], e["sink"]): e["count"] for e in base["entries"]}
    offenders, shrunk = [], []
    for (fp, sink), old in sorted(allowed.items()):
        now = counts.get(fp + "\t" + sink, 0)
        if now > old:
            offenders.append(f"{fp} {sink}: 现 {now} > 基线 {old}")
        elif now < old:
            shrunk.append(f"{fp} {sink}: 基线 {old} → 现 {now}")
    for key in sorted(counts):
        fp, sink = key.split("\t")
        if (fp, sink) not in allowed:
            offenders.append(f"{fp} {sink}: 现 {counts[key]} > 基线 0（新条目）")
    for s in shrunk:
        print("  收缩（好）：", s)
    for o in offenders:
        print("  NEW ", o)
    if offenders:
        sys.exit("数据流门未过：产品面 definite 超出基线（人工确认后 --update-baseline）")
    print("TAINT-GATE OK")


if __name__ == "__main__":
    main(sys.argv[1:])
