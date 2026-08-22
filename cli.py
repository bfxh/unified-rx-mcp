#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""unified-rx CLI —— 独立于 AI 模型的工具入口。

模型 API 停了，MCP 依然能干活：本 CLI 直接调用 server 内部函数，
不经任何 LLM / 网关 / 对话层。

子命令：
  scan PATH [--max-files N]
      静态扫描聚合（bug_scan + std_check + vuln_scan），输出 JSON/Markdown
  stats [--top N]
      调用/耗时/token 汇总（总调用、总耗时、最慢工具 TOP、按工具 TOP）
  track PATH [--repo R] [--min-severity error] [--close-stale]
      扫描发现问题 → 自动开 GitHub issue（指纹去重）→ 状态文件跟踪
      --close-stale: 重扫后已消失的 open issue 自动关闭
  schedule [--roots A;B] [--interval 600] [--repo R] [--min-severity error]
      常驻调度：定时 索引 + 扫描 + 自动跟踪（模型不在也跑）
      Ctrl+C 退出；日志 ~/.unified-rx/schedule.log
  denoise TEXT
      文本去废话（与 MCP denoise 工具同逻辑，命令行独立可用）

用法示例：
  python cli.py scan D:/开发/unified-rx-mcp
  python cli.py stats --top 10
  python cli.py track D:/开发/unified-rx-mcp --repo bfxh/unified-rx-mcp
  python cli.py schedule --roots "D:/开发/unified-rx-mcp;D:/开发/reasonix-src" --interval 600
  python cli.py denoise "好的，没问题。首先我想说的是……"
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent)
STATE_FILE = Path.home() / ".unified-rx" / "tracked-issues.json"
SCHEDULE_LOG = Path.home() / ".unified-rx" / "schedule.log"
STATS_FILE = Path.home() / ".unified-rx" / "stats.json"

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def ensure_mcp_python() -> None:
    """本 CLI 依赖 server（mcp SDK）。当前解释器缺 mcp 时自动换到带 mcp 的解释器重跑。"""
    try:
        import mcp  # noqa: F401
        return
    except ImportError:
        pass
    home = Path.home()
    candidates = [sys.executable]
    for ver in ("Python311", "Python312", "Python313", "Python310"):
        p = home / "AppData" / "Local" / "Programs" / "Python" / ver / "python.exe"
        if p.exists():
            candidates.append(str(p))
    for py in candidates[1:]:
        try:
            r = subprocess.run([py, "-c", "import mcp"],
                               capture_output=True, timeout=30)
            if r.returncode == 0:
                print(f"[cli] 当前解释器缺 mcp，切换到 {py}")
                os.execv(py, [py] + sys.argv)
        except Exception:
            continue
    print("[cli] 错误：找不到带 mcp SDK 的 Python（server.py 依赖 mcp）")
    sys.exit(3)


def _import_server():
    sys.path.insert(0, ROOT)
    import server  # noqa: F401
    return server


def _tc_text(result) -> str:
    if isinstance(result, list):
        return "".join(getattr(t, "text", str(t)) for t in result)
    return str(result)


# ── scan ────────────────────────────────────────────────────────────────
def cmd_scan(args: argparse.Namespace) -> int:
    server = _import_server()
    path = args.path
    out: dict = {"path": path, "generated_at": datetime.now().isoformat(timespec="seconds")}
    for name, fn, arg in (
        ("bug_scan", server._tool_bug_scan, {"path": path, "max_files": args.max_files}),
        ("std_check", server._tool_std_check, {"path": path, "max_files": args.max_files}),
        ("vuln_scan", server._tool_vuln_scan, {"path": path, "max_files": args.max_files}),
    ):
        t0 = time.perf_counter()
        try:
            r = fn(arg)
            text = _tc_text(r)
            out[name] = json.loads(text) if text.startswith("{") else {"raw": text[:500]}
        except Exception as exc:  # noqa: BLE001
            out[name] = {"error": str(exc)[:300]}
        out[name]["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    for name in ("bug_scan", "std_check", "vuln_scan"):
        d = out.get(name) or {}
        errs = d.get("severity_counts", {}).get("error", 0)
        warns = d.get("severity_counts", {}).get("warn", 0)
        print(f"[{name}] files={d.get('files', '-')} issues={d.get('issue_count', '-')} "
              f"error={errs} warn={warns} elapsed={d.get('elapsed_ms')}ms")
        if errs or warns:
            for i in (d.get("issues") or []):
                if i.get("severity") in ("error", "warn"):
                    print(f"  [{i['severity']}] {i.get('file')}:{i.get('line')} "
                          f"{i.get('rule')}: {(i.get('msg') or '')[:120]}")
    return 0


# ── stats ───────────────────────────────────────────────────────────────
def cmd_stats(args: argparse.Namespace) -> int:
    if not STATS_FILE.exists():
        print(f"[stats] 无统计文件：{STATS_FILE}")
        return 1
    rows = json.loads(STATS_FILE.read_text(encoding="utf-8", errors="replace"))
    if not isinstance(rows, list):
        rows = [rows]
    top = args.top
    n = len(rows)
    total_ms = sum(float(r.get("duration_ms", 0)) for r in rows)
    total_in = sum(int(r.get("tokens_in", 0)) for r in rows)
    total_out = sum(int(r.get("tokens_out", 0)) for r in rows)
    by_tool: dict[str, dict] = {}
    for r in rows:
        t = r.get("tool") or r.get("action") or "?"
        b = by_tool.setdefault(t, {"calls": 0, "ms": 0.0, "in": 0, "out": 0, "err": 0})
        b["calls"] += 1
        b["ms"] += float(r.get("duration_ms", 0))
        b["in"] += int(r.get("tokens_in", 0))
        b["out"] += int(r.get("tokens_out", 0))
        if r.get("ok") is False:
            b["err"] += 1
    print(f"[stats] 总记录 {n}，总耗时 {total_ms/1000:.1f}s，"
          f"tokens_in≈{total_in}，tokens_out≈{total_out}")
    print(f"[stats] 最慢工具 TOP{top}（平均/最大耗时 ms）：")
    slow = sorted(by_tool.items(), key=lambda kv: -kv[1]["ms"] / kv[1]["calls"])[:top]
    for t, b in slow:
        avg = b["ms"] / max(b["calls"], 1)
        print(f"  {t:28s} 调用={b['calls']:5d} 平均={avg:8.1f} 总={b['ms']/1000:7.1f}s "
              f"tok_in={b['in']} tok_out={b['out']} err={b['err']}")
    print(f"[stats] 调用最频繁 TOP{top}：")
    freq = sorted(by_tool.items(), key=lambda kv: -kv[1]["calls"])[:top]
    for t, b in freq:
        print(f"  {t:28s} 调用={b['calls']:5d} 平均={b['ms']/max(b['calls'],1):8.1f}ms")
    if args.json:
        print(json.dumps({"total": n, "total_ms": total_ms, "tokens_in": total_in,
                          "tokens_out": total_out,
                          "by_tool": by_tool}, ensure_ascii=False))
    return 0


# ── GitHub 自动问题跟踪 ────────────────────────────────────────────────
def _github_token() -> str:
    tok = os.environ.get("GITHUB_TOKEN", "")
    if tok:
        return tok
    try:
        r = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True, text=True, timeout=15)
        for line in (r.stdout or "").splitlines():
            if line.startswith("password="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def _gh_request(method: str, url: str, token: str, body: dict | None = None) -> dict:
    import urllib.request
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "unified-rx-cli")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        return {"error": f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:300]}


def _fingerprint(issue: dict) -> str:
    key = f"{issue.get('file')}:{issue.get('line')}:{issue.get('rule')}:{(issue.get('msg') or '')[:100]}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                          encoding="utf-8")


def _collect_issues(server, path: str, min_severity: str) -> list[dict]:
    """扫描并收集 >= min_severity 的问题。
    兼容两套词汇（2026-08-23 契约梳理）：
      bug_scan 系: error/warn/info/hint
      std_check 系: Critical/warning/suggestion（首字母大写历史契约）
    """
    order = {"error": 4, "critical": 4, "warn": 3, "warning": 3,
             "info": 2, "suggestion": 2, "hint": 1}
    min_lv = order.get(min_severity.lower(), 4)
    issues: list[dict] = []
    for fn, arg in (
        (server._tool_bug_scan, {"path": path, "max_files": 200}),
        (server._tool_std_check, {"path": path, "max_files": 200}),
    ):
        try:
            r = fn(arg)
            text = _tc_text(r)
            data = json.loads(text) if text.startswith("{") else {}
            for i in (data.get("issues") or []):
                if order.get(str(i.get("severity", "")).lower(), 0) >= min_lv:
                    issues.append(i)
        except Exception as exc:  # noqa: BLE001
            print(f"[track] {fn.__name__} 失败: {exc}")
    return issues


def cmd_track(args: argparse.Namespace) -> int:
    server = _import_server()
    token = _github_token()
    repo = args.repo
    # 防御：repo 只允许 owner/name 格式（防路径注入到 api.github.com URL）
    import re as _re
    if not _re.fullmatch(r"[\w.-]+/[\w.-]+", repo or ""):
        print(f"[track] repo 格式非法（须 owner/name）：{repo}")
        return 2
    state = _load_state()
    issues = _collect_issues(server, args.path, args.min_severity)
    print(f"[track] {args.path} → {len(issues)} 个 ≥{args.min_severity} 问题")
    if args.dry_run:
        for i in issues:
            print(f"  [dry-run] 将开 issue: {i.get('rule')} @ "
                  f"{i.get('file')}:{i.get('line')} — "
                  f"{(i.get('msg') or '')[:100]}")
        print("[track] dry-run 结束（未实际创建）")
        return 0
    if not token:
        print("[track] 无 GitHub token（GITHUB_TOKEN 或 git credential）——只更新本地状态，不开 issue")
    by_fp = {_fingerprint(i): i for i in issues}
    now = datetime.now().isoformat(timespec="seconds")
    opened = updated = closed = 0
    # 1) 新问题 → 开 issue
    for fp, issue in by_fp.items():
        rec = state.get(fp)
        if rec:
            rec["last_seen"] = now
            rec["root"] = args.path
            updated += 1
            continue
        if not token:
            state[fp] = {"root": args.path, "state": "local", "last_seen": now,
                         "issue": issue}
            continue
        title = f"[rx-scan] {issue.get('rule')} @ {issue.get('file')}:{issue.get('line')}"
        body = (f"**自动跟踪**（unified-rx CLI track，模型不在也跑）\n\n"
                f"- 路径: `{issue.get('file')}:{issue.get('line')}`\n"
                f"- 规则: `{issue.get('rule')}`\n"
                f"- 级别: {issue.get('severity')}\n"
                f"- 时间: {now}\n\n"
                f"```\n{(issue.get('msg') or '')[:500]}\n```\n\n"
                f"```\n{(issue.get('snippet') or '')[:300]}\n```\n\n"
                f"扫描根: `{args.path}`\n指纹: `{fp}`")
        res = _gh_request("POST", f"https://api.github.com/repos/{repo}/issues",
                          token, {"title": title[:200], "body": body})
        if res.get("error"):
            print(f"[track] 开 issue 失败: {res['error'][:200]}")
            continue
        state[fp] = {"root": args.path, "state": "open", "last_seen": now,
                     "issue_url": res.get("html_url"), "number": res.get("number"),
                     "issue": issue}
        print(f"[track] 已开 issue #{res.get('number')}: {res.get('html_url')}")
        opened += 1
    # 2) --close-stale：本地 open 的 issue 在当前扫描中消失 → 关闭
    if args.close_stale and token:
        for fp, rec in list(state.items()):
            if rec.get("state") != "open" or rec.get("root") != args.path:
                continue
            if fp in by_fp:
                continue
            if not rec.get("number"):
                state[fp]["state"] = "gone"
                continue
            res = _gh_request("PATCH",
                              f"https://api.github.com/repos/{repo}/issues/{rec['number']}",
                              token, {"state": "closed"})
            if res.get("error"):
                print(f"[track] 关闭 #{rec['number']} 失败: {res['error'][:200]}")
            else:
                state[fp]["state"] = "closed"
                print(f"[track] 已关闭 #{rec['number']}（问题消失）")
                closed += 1
    _save_state(state)
    print(f"[track] 新开={opened} 更新={updated} 关闭={closed} "
          f"状态文件={STATE_FILE}")
    return 0


# ── schedule（常驻调度）────────────────────────────────────────────────
def cmd_schedule(args: argparse.Namespace) -> int:
    server = _import_server()
    token = _github_token()
    import re as _re
    if not _re.fullmatch(r"[\w.-]+/[\w.-]+", args.repo or ""):
        print(f"[schedule] repo 格式非法（须 owner/name）：{args.repo}")
        return 2
    roots = [r.strip() for r in args.roots.split(";") if r.strip()]
    if not roots:
        roots = [os.getcwd()]
    interval = max(30, args.interval)
    log = SCHEDULE_LOG
    def log_line(msg: str) -> None:
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
        print(line, flush=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    log_line(f"schedule 启动：roots={roots} interval={interval}s "
             f"token={'有' if token else '无（仅本地）'} repo={args.repo}")
    while True:
        for root in roots:
            if not Path(root).exists():
                log_line(f"  跳过（不存在）：{root}")
                continue
            t0 = time.perf_counter()
            # 1) 增量索引（代码跟踪：变更感知）
            try:
                server._tool_cb_index({"path": root})
            except Exception as exc:  # noqa: BLE001
                log_line(f"  索引失败 {root}: {exc}")
            # 2) 扫描
            issues = _collect_issues(server, root, args.min_severity)
            # 3) 自动跟踪
            state = _load_state()
            n_new = 0
            now = datetime.now().isoformat(timespec="seconds")
            if token:
                repo = args.repo
                for i in issues:
                    fp = _fingerprint(i)
                    rec = state.get(fp)
                    if rec:
                        rec["last_seen"] = now
                        continue
                    title = (f"[rx-scan] {i.get('rule')} @ "
                             f"{i.get('file')}:{i.get('line')}")
                    body = (f"**schedule 自动跟踪**（{now}）\n\n"
                            f"- 路径: `{i.get('file')}:{i.get('line')}`\n"
                            f"- 规则: `{i.get('rule')}` 级别: {i.get('severity')}\n\n"
                            f"```\n{(i.get('msg') or '')[:400]}\n```\n\n扫描根: `{root}`")
                    res = _gh_request("POST",
                                      f"https://api.github.com/repos/{repo}/issues",
                                      token, {"title": title[:200], "body": body})
                    if not res.get("error"):
                        state[fp] = {"root": root, "state": "open",
                                     "last_seen": now,
                                     "issue_url": res.get("html_url"),
                                     "number": res.get("number"), "issue": i}
                        n_new += 1
            else:
                for i in issues:
                    fp = _fingerprint(i)
                    if fp not in state:
                        state[fp] = {"root": root, "state": "local",
                                     "last_seen": now, "issue": i}
            _save_state(state)
            dt = (time.perf_counter() - t0) * 1000
            log_line(f"  {root}: {len(issues)} 个 ≥{args.min_severity} 问题，"
                     f"新开 {n_new}，耗时 {dt:.0f}ms")
        time.sleep(interval)


# ── denoise ─────────────────────────────────────────────────────────────
def cmd_denoise(args: argparse.Namespace) -> int:
    server = _import_server()
    text = args.text or sys.stdin.read()
    try:
        r = server._denoise_text(text, aggressive=args.aggressive)
        print(json.dumps(r, ensure_ascii=False, indent=2))
    except AttributeError:
        print("[denoise] server 未提供 _denoise_text（版本过旧）")
        return 1
    return 0


def main() -> int:
    ensure_mcp_python()
    ap = argparse.ArgumentParser(description="unified-rx 独立 CLI（不经模型）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan", help="静态扫描聚合")
    p.add_argument("path")
    p.add_argument("--max-files", type=int, default=100)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_scan)
    p = sub.add_parser("stats", help="调用/token 汇总")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_stats)
    p = sub.add_parser("track", help="扫描→自动开/关 GitHub issue")
    p.add_argument("path")
    p.add_argument("--repo", default=os.environ.get("UNIFIED_RX_REPO", "bfxh/unified-rx-mcp"))
    p.add_argument("--min-severity", default="error",
                   choices=("error", "warn", "info", "hint"))
    p.add_argument("--close-stale", action="store_true")
    p.add_argument("--dry-run", action="store_true",
                   help="只预览将开的 issue，不实际创建")
    p.set_defaults(fn=cmd_track)
    p = sub.add_parser("schedule", help="常驻调度（定时索引+扫描+跟踪）")
    p.add_argument("--roots", default=os.environ.get("UNIFIED_RX_SCHEDULE_ROOTS", ""))
    p.add_argument("--interval", type=int, default=600)
    p.add_argument("--repo", default=os.environ.get("UNIFIED_RX_REPO", "bfxh/unified-rx-mcp"))
    p.add_argument("--min-severity", default="error",
                   choices=("error", "warn", "info", "hint"))
    p.set_defaults(fn=cmd_schedule)
    p = sub.add_parser("denoise", help="文本去废话（减 token）")
    p.add_argument("text", nargs="?", default="")
    p.add_argument("--aggressive", action="store_true")
    p.set_defaults(fn=cmd_denoise)
    args = ap.parse_args()
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        print("\n[cli] 已退出")
        return 0


if __name__ == "__main__":
    sys.exit(main())
