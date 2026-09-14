# -*- coding: utf-8 -*-
"""Mimosa 复审差量器（S138）：两份 report.md 的发现标题集差量。

用法：python scripts/audit_diff.py <old_report.md> <new_report.md> [--allow-added]
- 解析 `^### ` 标题行（形如 `### HIGH · 路径穿越 (tools/x.py:12)`）；
- 输出 消失 / 新增 清单；
- 退出码：0 = 无新增（复审收敛）；1 = 有新增（回归，需人工过目）；
  `--allow-added` 降级为只报告。
"""
import re
import sys


def heads(path):
    with open(path, encoding="utf-8") as f:
        return {m.strip() for m in re.findall(r"^### (.+)$", f.read(), re.M)}


def main(argv):
    allow = "--allow-added" in argv
    args = [a for a in argv if a != "--allow-added"]
    if len(args) != 2:
        sys.exit("用法: python scripts/audit_diff.py <old.md> <new.md> [--allow-added]")
    old, new = heads(args[0]), heads(args[1])
    gone, added = sorted(old - new), sorted(new - old)
    print(f"old={len(old)} new={len(new)} gone={len(gone)} added={len(added)}")
    for g in gone:
        print("  GONE ", g)
    for a in added:
        print("  ADDED", a)
    if added and not allow:
        sys.exit("复审出现新增发现（回归需人工过目）")
    print("AUDIT-DIFF OK")


if __name__ == "__main__":
    main(sys.argv[1:])
