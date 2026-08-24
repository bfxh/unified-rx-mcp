# -*- coding: utf-8 -*-
"""tools/ops.py —— 运维域（7 工具）：backup / cost_report / scan_log / usage_stats / trend_analysis / project_health / lesson_stats

收敛自旧版 backup/rollback + cost_report + stats/telemetry/alarm/scan_log。
纯 stdlib：zip 备份 + JSONL 统计 + 扫描日志。
2026-08-25 增强（用户：增加大量收集统计）：
- T1: registry.call 自动打点 stats.jsonl（cost_report 不再恒为 0）
- T2: scan_log 兼容旧版字符串 ts
- T4: usage_stats 工具使用统计（频率/耗时 TopN/时段分布）
- T5: trend_analysis 扫描趋势（按项目/按日 bug 数量变化）
- T6: project_health 项目健康度评分（bug/std/ui 综合 0-100）
- T7: lesson_stats 教训库统计（总量/关键词分布/召回热度）
"""
import os
import json
import time
import zipfile
import datetime
import collections
import re as _re

from registry import tool

_HOME = os.path.join(os.path.expanduser("~"), ".unified-rx")
_STATS_FILE = os.path.join(_HOME, "stats.jsonl")
_SCANLOG_FILE = os.path.join(_HOME, "scan-log.jsonl")
_LESSONS_FILE = os.path.join(_HOME, "lessons.jsonl")

_SKIP_BACKUP_DIRS = {".git", "node_modules", "__pycache__", "target", "dist",
                     ".unified-rx-index", ".pytest_cache"}
_SKIP_BACKUP_EXT = {".pyc", ".png", ".jpg", ".zip", ".exe", ".tar.gz"}


def _load_jsonl(path):
    """读 JSONL，容错（旧版 ts 字符串/坏行跳过）。"""
    recs = []
    if not os.path.exists(path):
        return recs
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except Exception:
                    pass
    except OSError:
        pass
    return recs


def _norm_ts(v):
    """兼容：ts 可能是 int 或 'YYYY-MM-DD HH:MM:SS' 字符串。返回 int 或 None。"""
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y%m%d-%H%M%S"):
            try:
                return int(datetime.datetime.strptime(v, fmt).timestamp())
            except ValueError:
                continue
    return None


@tool("backup", "每日备份与回溯（zip 快照 + rollback）", "ops",
      {"type": "object",
       "properties": {
           "action": {"type": "string", "description": "backup/list/rollback（默认 list）"},
           "root": {"type": "string", "description": "项目根（必填）"},
           "keep": {"type": "integer", "description": "保留快照份数（默认 7）"},
           "date": {"type": "string", "description": "rollback 用：YYYYMMDD 快照日期"},
       },
       "required": []})
def backup(root=None, action="list", keep=7, date=None):
    if not root:
        return {"error": "root 必填（项目根目录）"}
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
                "note": "registry.call 自动打点（每次调用写 stats.jsonl）"}
    recs = _load_jsonl(_STATS_FILE)
    by_tool = {}
    total_ms = 0
    for r in recs:
        t = r.get("tool", "?")
        by_tool.setdefault(t, {"calls": 0, "ms": 0})
        by_tool[t]["calls"] += 1
        by_tool[t]["ms"] += r.get("duration_ms", 0)
        total_ms += r.get("duration_ms", 0)
    rows = [{"tool": t, **v} for t, v in sorted(by_tool.items(), key=lambda x: -x[1]["calls"])]
    total_calls = len(recs)
    est_cost = total_calls * price * 0.001
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
    recs = _load_jsonl(_SCANLOG_FILE)
    if action == "trend":
        # T2/T4：兼容旧版字符串 ts
        by_day = {}
        for r in recs:
            ts = _norm_ts(r.get("ts"))
            if ts is None:
                continue
            d = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            by_day[d] = by_day.get(d, 0) + 1
        return {"trend": [{"day": d, "count": c} for d, c in sorted(by_day.items())[-14:]]}
    if root:
        recs = [r for r in recs if r.get("root") == root]
    return {"total": len(recs), "logs": recs[:limit]}


@tool("usage_stats", "工具使用统计（频率/耗时 TopN/时段分布）", "ops",
      {"type": "object",
       "properties": {
           "top": {"type": "integer", "description": "TopN（默认 10）"},
           "days": {"type": "integer", "description": "统计最近 N 天（默认 0=全部）"},
       },
       "required": []})
def usage_stats(top=10, days=0):
    """T4：基于 stats.jsonl 的工具使用统计。"""
    recs = _load_jsonl(_STATS_FILE)
    if days > 0:
        cutoff = int(time.time()) - days * 86400
        recs = [r for r in recs if _norm_ts(r.get("ts")) is not None and _norm_ts(r.get("ts")) >= cutoff]
    if not recs:
        return {"note": "暂无调用记录（registry.call 自动打点已启用）", "total_calls": 0}
    by_tool = collections.Counter()
    by_ms = collections.Counter()
    by_hour = collections.Counter()
    for r in recs:
        t = r.get("tool", "?")
        by_tool[t] += 1
        by_ms[t] += r.get("duration_ms", 0)
        ts = _norm_ts(r.get("ts"))
        if ts is not None:
            by_hour[datetime.datetime.fromtimestamp(ts).hour] += 1
    freq = [{"tool": t, "calls": c} for t, c in by_tool.most_common(top)]
    slow = [{"tool": t, "total_ms": m, "avg_ms": m // by_tool[t]} for t, m in by_ms.most_common(top)]
    hours = [{"hour": h, "calls": c} for h, c in sorted(by_hour.items())]
    return {
        "total_calls": len(recs),
        "freq_top": freq,
        "slowest_top": slow,
        "hourly_distribution": hours,
        "days": days or "全部",
    }


@tool("trend_analysis", "扫描趋势（按项目/按日 bug 数量变化）", "ops",
      {"type": "object",
       "properties": {
           "root": {"type": "string", "description": "按项目过滤（可选）"},
           "days": {"type": "integer", "description": "最近 N 天（默认 14）"},
       },
       "required": []})
def trend_analysis(root=None, days=14):
    """T5：扫描记录的趋势分析（bug 数量随时间的增减）。"""
    recs = _load_jsonl(_SCANLOG_FILE)
    cutoff = int(time.time()) - int(days) * 86400
    by_day = collections.defaultdict(lambda: {"scans": 0, "issues": 0, "ok": 0, "fail": 0})
    for r in recs:
        ts = _norm_ts(r.get("ts"))
        if ts is None or ts < cutoff:
            continue
        if root and root not in str(r.get("root", "")):
            continue
        d = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        by_day[d]["scans"] += 1
        # 尝试解析 issues 数（summary 里 "issues=N" 或 record 里 issues 字段）
        sm = str(r.get("summary", ""))
        m = re_search_issues(sm)
        if m:
            by_day[d]["issues"] += int(m.group(1))
        if r.get("ok"):
            by_day[d]["ok"] += 1
        else:
            by_day[d]["fail"] += 1
    series = [{"day": d, **v} for d, v in sorted(by_day.items())]
    # 趋势判断：最后 3 天 vs 前 3 天
    trend = "stable"
    if len(series) >= 6:
        recent = sum(s["issues"] for s in series[-3:])
        prev = sum(s["issues"] for s in series[-6:-3])
        if recent < prev:
            trend = "improving"
        elif recent > prev:
            trend = "worsening"
    return {"days": days, "trend": trend, "series": series, "total_scans": sum(s["scans"] for s in series)}


def re_search_issues(text):
    """从 summary 提取 issues=N。"""
    return _re.search(r"issues=(\d+)", text)


@tool("project_health", "项目健康度评分（bug/std/ui 综合 0-100）", "ops",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "项目根目录"},
           "max_files": {"type": "integer", "description": "扫描上限（默认 100）"},
       },
       "required": ["path"]})
def project_health(path, max_files=100):
    """T6：跑三路扫描 → 综合健康分（100 - 加权问题数）。"""
    from . import scan as _scan
    bug = _scan.bug_scan(path, max_files)
    std = _scan.std_check(path, max_files)
    ui = _scan.ui_check(path, max_files)
    b = bug.get("total", 0)
    s = std.get("total", 0)
    u = ui.get("total", 0)
    # 高严重度加权
    sev = bug.get("by_severity", {})
    high = sev.get("high", 0)
    score = max(0, min(100, 100 - b * 1 - s * 2 - u * 3 - high * 5))
    grade = "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D" if score >= 40 else "F"
    return {
        "path": path,
        "score": score, "grade": grade,
        "breakdown": {"bug_total": b, "bug_high": high, "std": s, "ui": u},
        "note": "100 - bug×1 - std×2 - ui×3 - high×5（clamp 0-100）",
    }


@tool("lesson_stats", "教训库统计（总量/关键词分布/召回热度）", "ops",
      {"type": "object",
       "properties": {"top": {"type": "integer", "description": "关键词 TopN（默认 10）"}},
       "required": []})
def lesson_stats(top=10):
    """T7：教训库的统计视图。"""
    lessons = _load_jsonl(_LESSONS_FILE)
    if not lessons:
        return {"total": 0, "note": "教训库为空"}
    kw = collections.Counter()
    total_len = 0
    for l in lessons:
        text = str(l.get("text", ""))
        total_len += len(text)
        for w in _re.findall(r"[a-z][a-z0-9_]{3,}", text.lower()):
            kw[w] += 1
        for seg in _re.findall(r"[\u4e00-\u9fff]{2,4}", text):
            kw[seg] += 1
    return {
        "total": len(lessons),
        "avg_len": round(total_len / len(lessons), 1),
        "top_keywords": [{"kw": k, "count": c} for k, c in kw.most_common(top)],
        "latest": lessons[-5:][::-1],
    }
