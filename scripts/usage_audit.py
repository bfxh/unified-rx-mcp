"""scripts/usage_audit.py —— 调用账本审计（S172）：归因量化 + 保留期清理 + 持久总账。

三件事（与 tools.ops.stats_maintenance 同一段实现，本脚本是它的**报告出口**）：
  ① **归因**：按 agent（=宿主 initialize 的 clientInfo.name）汇总"调了什么、多少次、
     耗时多少"——近窗口看原始账本，全期看 rollup 总账；
  ② **量化**：把超出保留期（默认 7 天，UNIFIED_RX_STATS_RETENTION_DAYS 可调）的原始记录
     合并进 `stats-rollup.json`（键=ISO周|agent|tool，同键累加——**后续量化可持续合并**）；
  ③ **清理**：原始 stats.jsonl / 分片只留保留期内的记录（原子改写，空分片删除）。

server 每次启动也自动跑同一段维护（tools.ops.stats_maintenance）；本脚本供手动审计与
定时任务使用。退出码：0=正常；1=维护或读账失败（如实报）。
"""
import argparse
import collections
import json
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from tools import ops


def _agent_rows(recs, top):
    """近窗口（原始账本内）按 agent 汇总：调用量 / 耗时 / 最常用工具。"""
    calls = collections.Counter()
    ms = collections.Counter()
    by_tool: dict = {}
    for r in recs:
        a = str(r.get("agent") or "unattributed")
        calls[a] += 1
        ms[a] += int(r.get("duration_ms") or 0)
        by_tool.setdefault(a, collections.Counter())[str(r.get("tool") or "?")] += 1
    rows = []
    for a, c in calls.most_common(top):
        rows.append({
            "agent": a, "calls": c, "ms": ms[a],
            "top_tools": [{"tool": t, "calls": n}
                          for t, n in by_tool[a].most_common(5)],
        })
    return rows, len(calls)


def _rollup_rows(rollup_path, top):
    """全期（rollup 总账，含已删原始）按 agent 再聚合。"""
    p = pathlib.Path(rollup_path)
    if not p.is_file():
        return [], 0
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[usage-audit] rollup 读不了（{e}）——如实空报", file=sys.stderr)
        return [], 0
    calls = collections.Counter()
    ms = collections.Counter()
    for ent in doc.get("merged") or []:
        agent = str(ent.get("key", "|").split("|")[1]) if ent.get("key") else "?"
        calls[agent] += int(ent.get("calls") or 0)
        ms[agent] += int(ent.get("ms") or 0)
    rows = [{"agent": a, "calls": c, "ms": ms[a]} for a, c in calls.most_common(top)]
    return rows, len(calls)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, help="保留期天数（默认取 UNIFIED_RX_STATS_RETENTION_DAYS，缺 7）")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--rollup", help="总账路径覆盖（测试用）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    try:
        m = ops.stats_maintenance(retention_days=a.days, rollup_path=a.rollup)
    except OSError as e:
        print(f"FAIL usage-audit 维护失败：{e}")
        return 1

    recs = []
    for f in ops._stats_files():
        recs.extend(ops._load_jsonl(f))
    recent_rows, recent_agents = _agent_rows(recs, a.top)
    total_rows, total_agents = _rollup_rows(a.rollup or ops._rollup_path(), a.top)

    report = {
        "retention_days": a.days or int(os.environ.get(
            "UNIFIED_RX_STATS_RETENTION_DAYS", "7")),
        "pruned_into_rollup": m["pruned"], "kept_raw": m["kept"],
        "files_changed": m["files_changed"], "shards_deleted": m["shards_deleted"],
        "recent": {"agents": recent_agents, "raw_records": len(recs),
                   "per_agent": recent_rows},
        "all_time": {"agents": total_agents, "rollup_keys": m["rollup_keys"],
                     "per_agent": total_rows},
        "rollup": m["rollup"],
    }
    if a.json:
        print(json.dumps(report, ensure_ascii=False))
        return 0

    print(f"USAGE-AUDIT 保留期={report['retention_days']}天 "
          f"本次并入总账={m['pruned']} 原始剩={m['kept']} "
          f"改写文件={m['files_changed']} 删空分片={m['shards_deleted']}")
    print(f"近窗口（原始账本）智能体数={recent_agents}")
    for r in recent_rows:
        tools_s = ", ".join(f"{t['tool']}×{t['calls']}" for t in r["top_tools"][:3])
        print(f"  {r['agent']:<20} calls={r['calls']:<6} ms={r['ms']:<8} {tools_s}")
    print(f"全期（rollup 总账，含已删原始）智能体数={total_agents}")
    for r in total_rows:
        print(f"  {r['agent']:<20} calls={r['calls']:<6} ms={r['ms']}")
    print(f"总账 {m['rollup']}（keys={m['rollup_keys']}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
