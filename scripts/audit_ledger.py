# -*- coding: utf-8 -*-
"""审计账本（S147）：Mimosa 副本封印的**机器对账** + 时效门。

此前封印只记在 HARDENING §四·补 的手工表里——手工表会漂、也不拦"太久没审"。
本脚本把账本变成 spec/audit-ledger.json（机器可读）并对三件事判红：

1. **形状**：每条含 round/date/seal(sha256:前缀)/findings/verdict/head；
2. **对账**：HARDENING 表中含 `sha256:` 的行数 == 账本条数，且每条 seal 前缀
   在表中出现（表与账本不许各说各话）；
3. **时效**：最新审计距今 ≤ MAX_DAYS（默认 14）且 head 之后的提交数
   ≤ MAX_COMMITS（默认 60）——超期/超距即红；`--allow-stale` 显式放行
   （用于"先推、随后补审计"的既定节奏）。

用法：python -X utf8 scripts/audit_ledger.py [--allow-stale]
env：UNIFIED_RX_AUDIT_MAX_DAYS / UNIFIED_RX_AUDIT_MAX_COMMITS 覆盖阈值。
"""
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "spec" / "audit-ledger.json"
HARDENING = ROOT / "spec" / "HARDENING.md"


def _git(args):
    cp = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True,
                        text=True, encoding="utf-8", errors="replace", timeout=120)
    return (cp.stdout or "").strip(), cp.returncode


def load():
    doc = json.loads(LEDGER.read_text(encoding="utf-8"))
    entries = doc.get("entries") or []
    if not entries:
        sys.exit("AUDIT-LEDGER FAIL: 账本为空")
    return entries


def check_shape(entries):
    bad = []
    for e in entries:
        if not re.match(r"^sha256:[0-9a-f]{8}", e.get("seal", "")):
            bad.append(f"{e.get('round')}: seal 形状不对")
        try:
            datetime.date.fromisoformat(e.get("date", ""))
        except ValueError:
            bad.append(f"{e.get('round')}: date 非 ISO")
        if not isinstance(e.get("findings"), int):
            bad.append(f"{e.get('round')}: findings 非整数")
    return bad


def check_table(entries):
    text = HARDENING.read_text(encoding="utf-8")
    rows = [ln for ln in text.splitlines()
            if ln.startswith("|") and "sha256:" in ln]
    bad = []
    if len(rows) != len(entries):
        bad.append(f"表 {len(rows)} 行 != 账本 {len(entries)} 条")
    for e in entries:
        if e["seal"] not in text:
            bad.append(f"{e['round']}: seal {e['seal'][:20]}… 不在 HARDENING 表中")
    return bad


def check_freshness(entries):
    max_days = int(os.environ.get("UNIFIED_RX_AUDIT_MAX_DAYS", "14"))
    max_commits = int(os.environ.get("UNIFIED_RX_AUDIT_MAX_COMMITS", "60"))
    last = entries[-1]           # 账本按时间顺序追加（见 policy），末条=最新
    age = (datetime.date.today() - datetime.date.fromisoformat(last["date"])).days
    head = last.get("head") or ""
    behind = None
    if head:
        out, rc = _git(["rev-list", "--count", f"{head}..HEAD"])
        if rc == 0 and out.isdigit():
            behind = int(out)
    msgs = []
    if age > max_days:
        msgs.append(f"距今 {age} 天 > {max_days}")
    if behind is None:
        msgs.append(f"head『{head}』无法对账（ghost commit？）")
    elif behind > max_commits:
        msgs.append(f"审计后已 {behind} 个提交 > {max_commits}")
    return msgs, last, age, behind


def main(argv):
    entries = load()
    bad = check_shape(entries) + check_table(entries)
    msgs, last, age, behind = check_freshness(entries)
    print(f"AUDIT-LEDGER 条数={len(entries)} 最新={last['round']}@{last['date']} "
          f"age={age}d behind={behind} head={last.get('head') or '-'}")
    if bad:
        for b in bad:
            print("  SHAPE/TABLE:", b)
        sys.exit("AUDIT-LEDGER FAIL: 形状/对账不过")
    if msgs:
        for m in msgs:
            print("  STALE:", m)
        if "--allow-stale" not in argv:
            sys.exit("AUDIT-LEDGER FAIL: 审计过期（补做复审，或显式 --allow-stale）")
        print("AUDIT-LEDGER WARN-STALE（explicitly allowed）")
        return 0
    print("AUDIT-LEDGER OK")


if __name__ == "__main__":
    main(sys.argv[1:])
