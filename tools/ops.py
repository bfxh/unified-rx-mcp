# -*- coding: utf-8 -*-
"""tools/ops.py —— 运维域（3 工具）：backup / cost_report / scan_log

收敛自旧版 backup/rollback + cost_report + stats/telemetry/alarm/scan_log。
纯 stdlib：zip 备份 + JSONL 统计 + 扫描日志。
"""
import os
import json
import time
import zipfile
import datetime

from registry import tool

_HOME = os.path.join(os.path.expanduser("~"), ".unified-rx")
_STATS_FILE = os.path.join(_HOME, "stats.jsonl")
_SCANLOG_FILE = os.path.join(_HOME, "scan-log.jsonl")

_SKIP_BACKUP_DIRS = {".git", "node_modules", "__pycache__", "target", "dist",
                     ".unified-rx-index", ".pytest_cache"}
_SKIP_BACKUP_EXT = {".pyc", ".png", ".jpg", ".zip", ".exe", ".tar.gz"}


def _record_stats(tool_name, duration_ms, tokens_in=0, tokens_out=0):
    """成本统计打点（被 _call 自动调用）。"""
    try:
        os.makedirs(_HOME, exist_ok=True)
        with open(_STATS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "tool": tool_name, "duration_ms": int(duration_ms),
                "tokens_in": int(tokens_in), "tokens_out": int(tokens_out),
                "ts": int(time.time()),
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass


@tool("backup", "每日备份与回溯（zip 快照 + rollback）", "ops",
      {"type": "object",
       "properties": {
           "action": {"type": "string", "description": "backup/list/rollback（默认 list）"},
           "root": {"type": "string", "description": "项目根（必填）"},
           "keep": {"type": "integer", "description": "保留快照份数（默认 7）"},
           "date": {"type": "string", "description": "rollback 用：YYYYMMDD 快照日期"},
       },
       "required": ["root"]})
def backup(root, action="list", keep=7, date=None):
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        return {"error": f"不是目录: {root}"}
    backup_dir = os.path.join(root, "backups")
    if action == "backup":
        os.makedirs(backup_dir, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        out = os.path.join(backup_dir, f"backup-{stamp}.zip")

        def walk(d, arc_base):
            for r, dirs, files in os.walk(d):
                dirs[:] = [x for x in dirs if x not in _SKIP_BACKUP_DIRS]
                for fn in files:
                    if os.path.splitext(fn)[1].lower() in _SKIP_BACKUP_EXT:
                        continue
                    ap = os.path.join(r, fn)
                    yield ap, os.path.join(arc_base, os.path.relpath(ap, root))

        count = 0
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for ap, arc in walk(root, os.path.basename(root)):
                try:
                    zf.write(ap, arc)
                    count += 1
                except Exception:
                    pass
        # 清理旧快照
        snaps = sorted(f for f in os.listdir(backup_dir) if f.endswith(".zip"))
        removed = []
        while len(snaps) > max(1, int(keep)):
            old = os.path.join(backup_dir, snaps.pop(0))
            os.remove(old)
            removed.append(old)
        return {"ok": True, "file": out, "files": count, "removed": removed}
    if action == "list":
        if not os.path.isdir(backup_dir):
            return {"snapshots": []}
        snaps = []
        for f in sorted(os.listdir(backup_dir)):
            if f.endswith(".zip"):
                p = os.path.join(backup_dir, f)
                snaps.append({"file": f, "size": os.path.getsize(p),
                              "mtime": int(os.path.getmtime(p))})
        return {"root": root, "snapshots": snaps}
    if action == "rollback":
        if not date:
            return {"error": "rollback 需要 date（YYYYMMDD）"}
        snaps = [f for f in os.listdir(backup_dir) if f.endswith(".zip") and date in f] if os.path.isdir(backup_dir) else []
        if not snaps:
            return {"error": f"未找到 {date} 的快照"}
        snaps.sort()
        target = os.path.join(backup_dir, snaps[-1])
        return {"ok": True, "note": f"快照 {target} 存在；解压由用户/CI 执行（zip_slip 防护）",
                "snapshot": snaps[-1]}
    return {"error": f"未知 action: {action}"}


@tool("cost_report", "成本核算（调用次数+耗时，按工具汇总）", "ops",
      {"type": "object",
       "properties": {
           "action": {"type": "string", "description": "summary/status（默认 summary）"},
           "model": {"type": "string", "description": "模型单价键（估算用，默认 deepseek-chat）"},
       },
       "required": []})
def cost_report(action="summary", model="deepseek-chat"):
    prices = {"deepseek-chat": 0.002, "deepseek-reasoner": 0.004,
              "gpt-4o": 0.01, "claude-3.5-sonnet": 0.015}
    price = prices.get(model, 0.001)
    if action == "status":
        return {"stats_file": _STATS_FILE,
                "exists": os.path.exists(_STATS_FILE),
                "note": "每次工具调用自动打点（_record_stats）"}
    recs = []
    if os.path.exists(_STATS_FILE):
        try:
            with open(_STATS_FILE, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            recs.append(json.loads(line))
                        except Exception:
                            pass
        except OSError:
            pass
    by_tool = {}
    total_ms = 0
    for r in recs:
        t = r.get("tool", "?")
        by_tool.setdefault(t, {"calls": 0, "ms": 0, "tokens_in": 0, "tokens_out": 0})
        by_tool[t]["calls"] += 1
        by_tool[t]["ms"] += r.get("duration_ms", 0)
        by_tool[t]["tokens_in"] += r.get("tokens_in", 0)
        by_tool[t]["tokens_out"] += r.get("tokens_out", 0)
        total_ms += r.get("duration_ms", 0)
    rows = [{"tool": t, **v} for t, v in sorted(by_tool.items(), key=lambda x: -x[1]["calls"])]
    total_calls = len(recs)
    est_cost = total_calls * price * 0.001  # 粗略估算（每调用 ~1k token 输入）
    return {
        "total_calls": total_calls, "total_ms": total_ms,
        "est_cost_usd": round(est_cost, 4),
        "by_tool": rows[:30],
        "model_price": price,
    }


@tool("scan_log", "扫描日志（查询/趋势，JSONL 落盘）", "ops",
      {"type": "object",
       "properties": {
           "action": {"type": "string", "description": "log/trend/record（默认 log）"},
           "root": {"type": "string", "description": "log 用：按项目过滤"},
           "limit": {"type": "integer", "description": "条数（默认 20）"},
           "record": {"type": "object", "description": "record 用：{root, summary}"},
       },
       "required": []})
def scan_log(action="log", root=None, limit=20, record=None):
    if action == "record":
        if not record:
            return {"error": "record 需要 record 对象"}
        try:
            os.makedirs(_HOME, exist_ok=True)
            with open(_SCANLOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": int(time.time()), **record}, ensure_ascii=False) + "\n")
            return {"ok": True}
        except OSError as e:
            return {"error": str(e)}
    recs = []
    if os.path.exists(_SCANLOG_FILE):
        try:
            with open(_SCANLOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            recs.append(json.loads(line))
                        except Exception:
                            pass
        except OSError:
            pass
    if action == "trend":
        by_day = {}
        for r in recs:
            d = datetime.datetime.fromtimestamp(r.get("ts", 0)).strftime("%Y-%m-%d")
            by_day[d] = by_day.get(d, 0) + 1
        return {"trend": [{"day": d, "count": c} for d, c in sorted(by_day.items())[-14:]]}
    if root:
        recs = [r for r in recs if r.get("root") == root]
    return {"total": len(recs), "logs": recs[:limit]}
