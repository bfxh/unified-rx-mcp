#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""plugin_cost.py —— 每轮工具 schema 税盘点（S141）。

解析 ~/.zcode/cli/rollout 里最新的 model-io 会话文件末行，按提供方分组统计
工具面体积与工具数——这部分是**每轮请求固定重发**的成本（与用不用无关）。
配合 skills/plugin-diet 的"按需挂载"策略使用。只读；无参数。
"""
import collections
import glob
import json
import os
import sys


def main():
    base = os.path.join(os.path.expanduser("~"), ".zcode", "cli", "rollout")
    files = glob.glob(os.path.join(base, "model-io-*.jsonl"))
    if not files:
        print("没有 model-io 会话文件（rollout 为空）")
        return 1
    newest = max(files, key=os.path.getmtime)
    last = None
    with open(newest, encoding="utf-8", errors="replace") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                last = ln
    if not last:
        print("最新会话文件为空")
        return 1
    rec = json.loads(last)
    body = rec.get("request", {}).get("body")
    if isinstance(body, str):
        body = json.loads(body)
    tools = body.get("tools") or []
    per = collections.Counter()
    cnt = collections.Counter()
    for t in tools:
        name = t.get("name") or (t.get("function") or {}).get("name") or "?"
        size = len(json.dumps(t, ensure_ascii=False))
        if name.startswith("mcp__plugin_"):
            plat = name[len("mcp__plugin_"):].split("_", 1)[0]
        elif name.startswith("mcp__"):
            plat = "[非插件MCP] " + name[5:].split("_")[0]
        else:
            plat = "[内置]"
        per[plat] += size
        cnt[plat] += 1
    total = sum(per.values())
    print(f"来源: {os.path.basename(newest)}")
    print(f"每轮固定工具税: {total / 1024:.0f}KB / {len(tools)} 个工具"
          f"（混合中英粗估 ≈{total / 3.5 / 1000:.0f}K token/轮）")
    for k, v in per.most_common(20):
        print(f"  {k:>28}  {v / 1024:6.1f}KB  x{cnt[k]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
