#!/usr/bin/env python3
"""plugin_cost.py —— 每轮工具 schema 税盘点（S141；S161 增「使用证据」列）。

解析 ~/.zcode/cli/rollout 里最新的 model-io 会话文件末行，按提供方分组统计
工具面体积与工具数——这部分是**每轮请求固定重发**的成本（与用不用无关）。
**新增（2026-09-22）**：从各会话响应的 `toolCalls` 里数真实调用次数——只有体积没有用量
判不出该关谁；「每轮纳税 × 从没用过」才是该关的那几个（实测：148KB/轮的 MCP 面累计只被
调用过 10 次，其中 desktop-commander 8 次、playwright/apollo/context7/microsoft-docs/
cloudflare 全 0）。
配合 skills/plugin-diet 的"按需挂载"策略使用。只读；无参数。
"""
import collections
import glob
import json
import os
import sys


def usage_counts():
    """工具名 → 累计真实调用次数（从所有会话的 response.toolCalls 里数）。"""
    hits = collections.Counter()
    base = os.path.join(os.path.expanduser("~"), ".zcode", "cli", "rollout")
    for path in glob.glob(os.path.join(base, "model-io-*.jsonl")):
        if "pytest" in path or "Temp" in path:
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = json.loads(ln)
                except ValueError:
                    continue
                for tc in ((rec.get("response") or {}).get("toolCalls") or []):
                    hits[tc.get("toolName") or tc.get("name") or "?"] += 1
    return hits


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
    calls = collections.Counter()
    usage = usage_counts()
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
        calls[plat] += usage.get(name, 0)
    total = sum(per.values())
    print(f"来源: {os.path.basename(newest)}")
    print(f"每轮固定工具税: {total / 1024:.0f}KB / {len(tools)} 个工具"
          f"（混合中英粗估 ≈{total / 3.5 / 1000:.0f}K token/轮）")
    print(f"{'提供方':>28}{'体积':>10}{'工具':>6}{'累计调用':>10}   建议")
    for k, v in per.most_common(20):
        c = calls[k]
        if k == "[内置]":
            hint = "不可关"
        elif c == 0:
            hint = "0 次调用 → 按需项，建议关"
        elif c < 20 and v / 1024 > 10:
            hint = f"仅 {c} 次却 {v / 1024:.0f}KB/轮 → 建议关或按需开"
        else:
            hint = "常用 → 常驻"
        print(f"{k:>28}  {v / 1024:6.1f}KB  x{cnt[k]:<4} {c:>8}   {hint}")
    spare = sum(v for k, v in per.items() if k != "[内置]" and calls[k] < 20 and v / 1024 > 4)
    if spare:
        print(f"\n按「0 次/极少调用」口径可关掉的 ≈{spare / 1024:.0f}KB/轮 "
              f"≈{spare / 3.5 / 1000:.1f}K token/轮（100 轮会话 ≈{spare / 3.5 * 100 / 1e6:.1f}M token）")
        print("开关：config.json → plugins.enabledPlugins 置 false，**重启生效**（见 skills/plugin-diet）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
