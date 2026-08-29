# -*- coding: utf-8 -*-
"""log_round.py —— 会话记录规范助手：每轮工作的推导/决策/证据入账。

用法：
  python bench/log_round.py --round S39 --task "做什么" \\
      --decision "为什么这样做" --evidence "实测数字/测试" --commit abc1234

追加到 spec/ROUNDLOG.md（跨项目通用：任何项目建 docs/ROUNDLOG.md 同格式）。
规范：每轮必须有 task/decision/evidence 三要素——缺 evidence 的轮次
视为未完成（与"表面光鲜"反向对齐）。
"""
import argparse
import os
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LOG = os.path.join(ROOT, "spec", "ROUNDLOG.md")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--round", required=True, help="轮次编号（S## / 自定义）")
    ap.add_argument("--task", required=True)
    ap.add_argument("--decision", required=True, help="关键决策与为什么")
    ap.add_argument("--evidence", required=True, help="实测数字/测试/文件出处")
    ap.add_argument("--commit", default="")
    ap.add_argument("--project", default="unified-rx-mcp")
    a = ap.parse_args()
    entry = (
        f"\n## {a.round} · {a.task}\n"
        f"- 项目：{a.project}｜时间：{datetime.now().isoformat(timespec='minutes')}\n"
        f"- 决策：{a.decision}\n"
        f"- 证据：{a.evidence}\n"
        + (f"- 提交：{a.commit}\n" if a.commit else "")
    )
    if not os.path.exists(LOG):
        with open(LOG, "w", encoding="utf-8", newline="") as f:
            f.write("# ROUNDLOG —— 每轮推导/决策/证据记录\n\n"
                    "规范：每轮三要素（任务/决策/证据）；本文件由 "
                    "bench/log_round.py 追加，人工可补充。\n")
    with open(LOG, "a", encoding="utf-8", newline="") as f:
        f.write(entry)
    print(f"[OK] {a.round} -> {os.path.relpath(LOG, ROOT)}")


if __name__ == "__main__":
    main()
