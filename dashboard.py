#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""unified-rx 运行仪表盘（零依赖 Web UI，证明 MCP 真在运行）。

纯标准库（http.server + json），无第三方依赖。读取 ~/.unified-rx/ 的
stats.json / scan-log.jsonl / telemetry.jsonl + 旁侧 tools.json，
提供 JSON API 与内嵌 HTML 仪表盘（3s 自动刷新）。

用法：
    python dashboard.py            # http://127.0.0.1:17300
    RX_DASH_PORT=9000 python dashboard.py
    RX_DASH_DATA=<dir> python dashboard.py   # 自定义数据目录
"""
import http.server
import json
import os
import sys
import time
from collections import Counter, defaultdict

PORT = int(os.environ.get("RX_DASH_PORT", "17300"))
DATA_DIR = os.environ.get("RX_DASH_DATA") or os.path.join(
    os.path.expanduser("~"), ".unified-rx")
HERE = os.path.dirname(os.path.abspath(__file__))
START_TS = time.time()

# 数据文件 → 最近 mtime（判断 server 是否活跃）
_FILES = ("stats.json", "scan-log.jsonl", "telemetry.jsonl", "repo-log.jsonl")


def _read_jsonl(path, limit):
    """读 JSONL 尾部 N 条（大文件只读尾——文件可能 GB 级）。"""
    out = []
    try:
        with open(path, "rb") as f:
            # 尾部流式读取：seek 到最后，往回读块
            f.seek(0, 2)
            size = f.tell()
            chunk = b""
            pos = size
            while pos > 0 and len(out) < limit * 4:
                read = min(65536, pos)
                pos -= read
                f.seek(pos)
                chunk = f.read(read) + chunk
                # 按行切出完整行
                lines = chunk.split(b"\n")
                chunk = lines[0]
                for ln in reversed(lines[1:]):
                    if ln.strip():
                        try:
                            out.append(json.loads(ln.decode("utf-8", "replace")))
                        except Exception:
                            pass
                        if len(out) >= limit * 4:
                            break
            return out[:limit]
    except OSError:
        return []


def _read_stats():
    """stats.json 全量聚合（本地文件，缓存友好）。"""
    p = os.path.join(DATA_DIR, "stats.json")
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            recs = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    total = len(recs)
    by_tool = Counter(r.get("tool", "?") for r in recs)
    dur_by_tool = defaultdict(list)
    for r in recs:
        d = r.get("duration_ms") or 0
        dur_by_tool[r.get("tool", "?")].append(d)
    return {
        "total": total,
        "by_tool": dict(by_tool.most_common(30)),
        "avg_ms": {t: round(sum(v) / len(v), 3) for t, v in dur_by_tool.items()},
    }


def _tools():
    """工具清单（tools.json 旁侧文件，不 import server——零耦合）。"""
    p = os.path.join(HERE, "tools.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d
    except (OSError, json.JSONDecodeError):
        return {"core_count": 0, "ext_count": 0, "total": 0,
                "core": [], "ext": []}


def _overview():
    """运行状态：数据新鲜度（server 活跃判定）+ 汇总。"""
    tools = _tools()
    stats = _read_stats()
    now = time.time()
    files = {}
    latest_ts = 0.0
    for name in _FILES:
        p = os.path.join(DATA_DIR, name)
        try:
            mt = os.path.getmtime(p)
            files[name] = {"mtime": mt, "age_s": round(now - mt, 1)}
            latest_ts = max(latest_ts, mt)
        except OSError:
            files[name] = {"mtime": 0, "age_s": -1}
    # 遥测心跳（daemon 循环活跃度）
    hbs = {}
    tel = _read_jsonl(os.path.join(DATA_DIR, "telemetry.jsonl"), 200)
    for r in tel:
        if r.get("kind") == "hb":
            hbs[r.get("loop", "?")] = r.get("ts", 0)
    return {
        "ok": True,
        "server_uptime_s": round(now - START_TS, 1),
        "data_latest_age_s": round(now - latest_ts, 1) if latest_ts else -1,
        "files": files,
        "heartbeats": hbs,
        "tools": {"core": tools.get("core_count", 0),
                  "ext": tools.get("ext_count", 0),
                  "total": tools.get("total", 0)},
        "stats_total": stats.get("total", 0),
    }


def _scanlog(limit=15):
    recs = _read_jsonl(os.path.join(DATA_DIR, "scan-log.jsonl"), limit)
    return [{"ts": r.get("ts"), "tool": r.get("tool"), "root": r.get("root"),
             "ok": r.get("ok"), "summary": (r.get("summary") or "")[:120]}
            for r in recs]


def _telemetry(limit=300):
    recs = _read_jsonl(os.path.join(DATA_DIR, "telemetry.jsonl"), limit)
    tools = [r for r in recs if r.get("kind") == "tool"]
    n = len(tools)
    err = sum(1 for r in tools if r.get("status") == "error")
    slow = sorted(tools, key=lambda r: -(r.get("wall_ms") or 0))[:8]
    return {
        "samples": n,
        "err_count": err,
        "err_rate": round(err / n, 3) if n else 0,
        "slowest": [{"tool": r.get("tool"), "ms": r.get("wall_ms"),
                     "status": r.get("status")} for r in slow],
    }


def _live(limit=20):
    """最近调用流（stats.json 尾部）。"""
    recs = _read_jsonl(os.path.join(DATA_DIR, "stats.json"), limit)
    # stats.json 是 JSON 数组而非 JSONL——用 read_stats 尾部
    if not recs:
        try:
            with open(os.path.join(DATA_DIR, "stats.json"), "r", encoding="utf-8") as f:
                allr = json.load(f)
            recs = allr[-limit:]
        except Exception:
            recs = []
    return [{"ts": r.get("ts"), "tool": r.get("tool"),
             "ms": round(r.get("duration_ms") or 0, 3)} for r in recs]


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静默（不刷屏）
        pass

    def _json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path == "/api/overview":
                return self._json(_overview())
            if path == "/api/tools":
                t = _tools()
                st = _read_stats()
                core = [{"name": n, "calls": st.get("by_tool", {}).get(n, 0)}
                        for n in t.get("core", [])]
                return self._json({"ok": True, "core": core,
                                   "ext": t.get("ext", [])})
            if path == "/api/scanlog":
                return self._json({"ok": True, "records": _scanlog()})
            if path == "/api/telemetry":
                return self._json({"ok": True, **_telemetry()})
            if path == "/api/live":
                return self._json({"ok": True, "records": _live()})
            if path == "/":
                body = _PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            return self._json({"ok": False, "error": f"未知路径 {path}"})
        except Exception as e:  # noqa: BLE001 —— API 单点异常不拖垮服务
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"})


_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>unified-rx 运行仪表盘</title>
<style>
  :root { --bg:#0d1117; --card:#161b22; --line:#30363d; --fg:#e6edf3;
          --dim:#8b949e; --ok:#3fb950; --warn:#d29922; --err:#f85149;
          --acc:#58a6ff; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--fg); font:14px/1.5 "Segoe UI",
         "Microsoft YaHei", sans-serif; padding:20px; }
  header { display:flex; align-items:center; gap:16px; flex-wrap:wrap;
           margin-bottom:18px; }
  h1 { font-size:20px; }
  .dot { width:10px; height:10px; border-radius:50%; background:var(--ok);
         display:inline-block; animation:pulse 2s infinite; }
  @keyframes pulse { 50% { opacity:.35; } }
  .pill { background:var(--card); border:1px solid var(--line); padding:4px 12px;
          border-radius:999px; font-size:13px; color:var(--dim); }
  .pill b { color:var(--fg); }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr));
          gap:14px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:14px; }
  .card h2 { font-size:14px; color:var(--dim); margin-bottom:10px;
             font-weight:600; }
  .bar { display:flex; align-items:center; gap:8px; margin:4px 0; }
  .bar .name { width:130px; text-align:right; color:var(--dim); font-size:12px;
               overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .bar .track { flex:1; background:#21262d; border-radius:4px; height:14px; }
  .bar .fill { height:14px; border-radius:4px; background:var(--acc);
               transition:width .5s; }
  .bar .num { width:56px; color:var(--dim); font-size:12px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  td, th { padding:4px 8px; border-bottom:1px solid var(--line); text-align:left; }
  .ok { color:var(--ok); } .warn { color:var(--warn); } .err { color:var(--err); }
  .live { max-height:280px; overflow-y:auto; font-family:Consolas,monospace;
          font-size:12px; }
  .live div { padding:2px 4px; border-bottom:1px dashed var(--line); }
  .muted { color:var(--dim); font-size:12px; }
  .stamp { margin-top:14px; color:var(--dim); font-size:11px; text-align:center; }
</style>
</head>
<body>
<header>
  <h1>&#9889; unified-rx 运行仪表盘</h1>
  <span class="dot" id="dot"></span>
  <span class="pill">工具 <b id="tools">&mdash;</b></span>
  <span class="pill">累计调用 <b id="calls">&mdash;</b></span>
  <span class="pill">数据新鲜度 <b id="fresh">&mdash;</b></span>
  <span class="pill">本页进程 <b id="uptime">&mdash;</b></span>
</header>
<div class="grid">
  <div class="card"><h2>&#128293; 工具调用热榜（TOP 10）</h2><div id="bars"></div></div>
  <div class="card"><h2>&#128225; 实时调用流（最近 20 条）</h2><div class="live" id="live"></div></div>
  <div class="card"><h2>&#128640; 遥测（最近 300 样本）</h2>
    <table><tr><th>指标</th><th>值</th></tr>
      <tr><td>样本</td><td id="tel_n">&mdash;</td></tr>
      <tr><td>错误率</td><td id="tel_err">&mdash;</td></tr>
      <tr><td>最慢工具</td><td id="tel_slow">&mdash;</td></tr>
    </table>
    <h2 style="margin-top:12px">&#128157; daemon 心跳</h2><div id="hbs" class="muted">&mdash;</div>
  </div>
  <div class="card"><h2>&#129513; 最近扫描（scan-log）</h2>
    <table><thead><tr><th>时间</th><th>工具</th><th>结果</th><th>摘要</th></tr></thead>
    <tbody id="scanlog"></tbody></table>
  </div>
</div>
<div class="stamp">unified-rx dashboard &middot; 数据目录 ~/.unified-rx &middot; 3s 自动刷新</div>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmtTs = ts => ts ? new Date(ts * 1000).toLocaleTimeString("zh-CN") : "&mdash;";
const fmtAge = s => s < 0 ? "&mdash;" : (s < 60 ? s + "s" : Math.round(s/60) + "m");
async function jget(p) { const r = await fetch(p); return r.json(); }
function bars(data) {
  const top = data.slice(0, 10);
  const max = Math.max(...top.map(t => t.calls), 1);
  document.getElementById("bars").innerHTML = top.map(t =>
    `<div class="bar"><span class="name" title="${esc(t.name)}">${esc(t.name)}</span>
     <span class="track"><span class="fill" style="width:${Math.round(t.calls/max*100)}%"></span></span>
     <span class="num">${t.calls}</span></div>`).join("");
}
function live(recs) {
  document.getElementById("live").innerHTML = recs.slice().reverse().map(r =>
    `<div>${fmtTs(r.ts)} &middot; ${esc(r.tool)} &middot; ${r.ms}ms</div>`).join("")
    || "<div class='muted'>暂无调用</div>";
}
function telemetry(t) {
  document.getElementById("tel_n").textContent = t.samples;
  document.getElementById("tel_err").textContent = t.samples
    ? (t.err_rate*100).toFixed(1) + "%（" + t.err_count + " 次）" : "&mdash;";
  document.getElementById("tel_slow").textContent = t.slowest.length
    ? t.slowest.map(s => `${esc(s.tool)} ${s.ms}ms`).join(" &middot; ") : "&mdash;";
}
function hbs(h) {
  const now = Date.now()/1000;
  const items = Object.entries(h).map(([k, ts]) =>
    `<span class="${now-ts<300?"ok":"err"}">${esc(k)} ${fmtAge(now-ts)}前</span>`)
    .join("&nbsp; ");
  document.getElementById("hbs").innerHTML = items
    || "<span class='muted'>无心跳记录</span>";
}
function scanlog(recs) {
  document.getElementById("scanlog").innerHTML = recs.map(r =>
    `<tr><td>${fmtTs(typeof r.ts === "string" ? Date.parse(r.ts)/1000 : r.ts)}</td>
     <td>${esc(r.tool)}</td>
     <td class="${r.ok ? "ok" : "err"}">${r.ok ? "OK" : "FAIL"}</td>
     <td class="muted">${esc(r.summary)}</td></tr>`).join("")
    || "<tr><td colspan=4 class='muted'>暂无扫描记录</td></tr>";
}
async function tick() {
  try {
    const [ov, tools, tel, sl, lv] = await Promise.all([
      jget("/api/overview"), jget("/api/tools"), jget("/api/telemetry"),
      jget("/api/scanlog"), jget("/api/live")]);
    document.getElementById("dot").style.background =
      (ov.data_latest_age_s >= 0 && ov.data_latest_age_s < 600) ? "var(--ok)" : "var(--warn)";
    document.getElementById("tools").textContent = ov.tools.total
      ? ov.tools.total + "（核心 " + ov.tools.core + "）" : "&mdash;";
    document.getElementById("calls").textContent = ov.stats_total;
    document.getElementById("fresh").textContent = fmtAge(ov.data_latest_age_s);
    document.getElementById("uptime").textContent = Math.round(ov.server_uptime_s) + "s";
    bars(tools.core); telemetry(tel); hbs(ov.heartbeats);
    scanlog(sl.records); live(lv.records);
  } catch (e) { /* 首帧可能失败，下轮重试 */ }
}
tick(); setInterval(tick, 3000);
</script>
</body>
</html>"""


def main() -> int:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), _Handler)
    print(f"unified-rx dashboard: http://127.0.0.1:{PORT}  (数据目录 {DATA_DIR})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
