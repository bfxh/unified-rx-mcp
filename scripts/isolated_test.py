#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""isolated_test —— MCP 隔离验收测试（第三方视角，不经 AI）。

为什么隔离：对接 AI 自测自己的 MCP 有盲区——自测护短、协议层问题看不见、
"调用成功"和"契约正确"混为一谈。本脚本把 server 当黑盒：
  - 独立进程 spawn（不经 AI / 网关 / 对话层 / stats 打点）
  - 原生 JSON-RPC 帧直连（stdio newline-delimited，与 Claude Code 等客户端同协议）
  - 全量契约验收（每个工具 schema 合法性：required ⊆ properties、描述非空、无占位符）
  - 代表性工具实调（固定参数表优先 + schema 自动生成兜底；危险/重型工具只验契约）
  - 性能基线（每次实调计时；>1s 标记 slow，>2s 标记 critical）
  - 独立报告 reports/ISOLATED_TEST_<date>.json + stdout 摘要 + 退出码（0=全过）

用法：
  python scripts/isolated_test.py            # 全量（契约 + 实调 + 性能）
  python scripts/isolated_test.py --calls-only   # 只跑代表实调（快）
  python scripts/isolated_test.py --json         # 只输出 JSON 报告路径
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)
SERVER = str(Path(ROOT) / "server.py")
SAMPLE_FILE = str(Path(ROOT) / "scripts" / "mcp_smoke.py")
SAMPLE_README = str(Path(ROOT) / "README.md")

# 控制台编码兜底（Windows GBK 控制台打印中文/路径不崩）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def find_python_with_mcp() -> str:
    """探测带 mcp SDK 的解释器（server.py 依赖 mcp 模块）。"""
    candidates = [sys.executable]
    home = Path.home()
    for ver in ("Python311", "Python312", "Python313", "Python310"):
        p = home / "AppData" / "Local" / "Programs" / "Python" / ver / "python.exe"
        if p.exists():
            candidates.append(str(p))
    for py in candidates:
        try:
            r = subprocess.run([py, "-c", "import mcp"],
                               capture_output=True, timeout=30)
            if r.returncode == 0:
                return py
        except Exception:
            continue
    return sys.executable  # 找不到则原样返回（让 server 报错暴露问题）

SLOW_MS = 1000.0      # >1s 慢工具
CRITICAL_MS = 2000.0  # >2s 临界慢

# ── 固定参数实调表（优先于 schema 自动生成；实调前按 schema 过滤未知字段）──
CALL_TABLE: dict[str, dict] = {
    "math_ops": {"action": "add", "a": 2, "b": 3},
    "text_ops": {"action": "reverse", "s": "abc"},
    "sort_search": {"action": "quick_sort", "arr": [3, 1, 2]},
    "fs_stat": {"path": ROOT},
    "fs_read": {"path": SAMPLE_README},
    "fs_list": {"path": ROOT, "depth": 1},
    "capability_manifest": {},
    "hallucination_guard": {"text": "server.py 位于仓库根目录", "root": ROOT},
    "denoise": {"text": "好的，没问题。首先我想说的是，这个功能确实是可以的。"
                        "换句话说，也就是说，它很好用。希望对你有所帮助！"},
    "ciopt_batch": {"func": "string_case_to_uppercase",
                    "input": ["hello", "world"], "arg": "s"},
    "dep_graph": {"path": str(Path(ROOT) / "scripts"), "max_files": 20},
    "user_sim": {"actions": [{"action": "wait", "ms": 30}]},
    "bug_scan": {"path": SAMPLE_FILE, "max_files": 3},
    "std_check": {"path": SAMPLE_FILE},
    "vuln_scan": {"path": SAMPLE_FILE, "max_files": 3},
    "ds_check": {"path": str(Path(ROOT) / "rx-core" / "src" / "lib.rs"), "max_files": 3},
    "predict_impact": {"root": str(Path(ROOT) / "scripts"), "symbol": "main", "file_hint": SAMPLE_FILE},
}

# ── schema 自动生成实调名单（安全只读/轻量；参数由 required 字段按类型生成）──
AUTO_CALL_NAMES: set[str] = {
    "cb_status", "ui_check", "scan_log", "lesson_recall", "file_dedup_state",
    "ide_health", "cost_report", "aether_probe", "tool_card", "ds_lookup",
    "ds_check", "layer_check", "alarm_check", "predict_impact", "skill_fetch",
    "denoise", "stats_now", "stats_status", "stats_top", "stats_query",
    "cost_now", "chatlog_search", "repo_health_status", "runtime_state",
    "game_check", "game_eval", "window_check", "media_probe", "backup_status",
    "scan_trend", "replay_status", "telemetry_status", "local_tools_list",
}
# 纯函数族（自动生成参数，验证不崩）——统计/校验/转换/素数/几何/JSON
AUTO_PREFIXES: tuple[str, ...] = (
    "stat_", "valid_", "prime_", "fib_", "conv_", "geo_", "json_", "str_",
    "math_", "sort_", "search_",
)

# ── 只契约验证（不实调）：写/状态/网络/重型/GUI/扩展
CONTRACT_ONLY_PREFIXES: tuple[str, ...] = (
    "fs_write", "backup", "rollback", "net_", "replay_", "quest_", "ide_",
    "game_", "blender_", "mesh_", "voxel", "half_edge", "geom_", "lsp_",
    "telemetry_", "pr_oracle_", "tautest_", "cae_", "ciopt_", "local_tools_",
    "repo_health", "media_", "stress_", "sage_", "failure_", "cov_",
    "pipeline", "parallel", "full_scan", "project_scan", "learn", "speculate",
    "multi_agent", "orchestrator", "window_", "realtime_", "daemon", "plugin_",
    "local_intel", "watch_once", "shadow_", "scan_cache", "predict_impact",
    "alarm_", "runtime_state", "game_eval", "skill_fetch", "chatlog_search",
    "tool_card", "cost_", "stats_", "scan_log", "lesson_", "cb_", "ide_health",
    "aether_", "ds_", "layer_check", "file_dedup", "ui_check", "vuln_scan",
    "std_check", "bug_scan", "denoise", "code_search", "search_", "facade",
    "selftest", "health", "status",
)


class IsolatedTest:
    def __init__(self, server: str = SERVER, calls_only: bool = False):
        self.server = server
        self.calls_only = calls_only
        self.proc: subprocess.Popen | None = None
        self.next_id = 100
        self.tools: list[dict] = []
        self.contract_fails: list[dict] = []
        self.contract_warns: list[dict] = []
        self.call_results: list[dict] = []
        self.protocol_ok = False

    # ── 协议层 ──
    def start(self) -> None:
        env = dict(os.environ)
        env["UNIFIED_RX_NO_STATS"] = "1"  # 隔离：不打点不污染统计
        env["UNIFIED_RX_SANDBOX"] = ROOT
        self.proc = subprocess.Popen(
            [find_python_with_mcp(), self.server],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env,
        )

    def send(self, obj: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def recv(self, timeout: float = 60.0) -> dict:
        assert self.proc and self.proc.stdout
        # Windows 下 select 不适用于管道——用线程读 + 超时
        lines: list[str] = []

        def _read():
            while True:
                raw = self.proc.stdout.readline()  # type: ignore
                if not raw:
                    break
                lines.append(raw.decode("utf-8", "replace"))
                if raw.endswith(b"\n"):
                    break

        import threading
        t = threading.Thread(target=_read, daemon=True)
        t.start()
        t.join(timeout)
        if not lines:
            err = (self.proc.stderr.read().decode("utf-8", "replace")
                   if self.proc.stderr else "")
            raise TimeoutError(f"recv 超时 {timeout}s, stderr: {err[:300]}")
        return json.loads(lines[0])

    def stop(self) -> None:
        if self.proc:
            self.proc.kill()
            self.proc = None

    def _call(self, name: str, arguments: dict) -> dict:
        """同步工具调用（自带超时）。返回 {ok, duration_ms, error, text}"""
        self.next_id += 1
        rid = self.next_id
        t0 = time.perf_counter()
        try:
            self.send({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}})
            resp = self.recv(timeout=90.0)
        except Exception as exc:
            return {"ok": False, "duration_ms": (time.perf_counter() - t0) * 1000,
                    "error": f"transport: {exc}", "text": ""}
        dt = (time.perf_counter() - t0) * 1000
        if resp.get("id") != rid:
            return {"ok": False, "duration_ms": dt, "error": f"id mismatch: {resp.get('id')}",
                    "text": ""}
        result = resp.get("result") or {}
        if result.get("isError"):
            text = "".join(c.get("text", "") for c in result.get("content", []))
            return {"ok": False, "duration_ms": dt, "error": text[:300], "text": text}
        text = "".join(c.get("text", "") for c in result.get("content", []))
        return {"ok": True, "duration_ms": dt, "error": "", "text": text}

    # ── 契约验收 ──
    def contract_check(self) -> None:
        bad_placeholders = ("todo", "lorem", "占位", "待补充", "xxx", "placeholder")
        for t in self.tools:
            name = t.get("name", "")
            desc = t.get("description", "") or ""
            schema = t.get("inputSchema") or {}
            problems = []
            if not name:
                problems.append("空工具名")
            if len(desc) < 4:
                problems.append("描述过短")
            if schema.get("type") != "object":
                problems.append("inputSchema.type != object")
            props = schema.get("properties")
            if not isinstance(props, dict):
                problems.append("properties 缺失")
            req = schema.get("required")
            if req is not None and not isinstance(req, list):
                problems.append("required 非数组")
            if isinstance(props, dict) and isinstance(req, list):
                for r in req:
                    if r not in props:
                        problems.append(f"required 字段 {r} 不在 properties 中")
            if desc.lower().startswith(bad_placeholders):
                problems.append("描述含占位符")
            if problems:
                self.contract_fails.append({"tool": name, "problems": problems})
            elif len(desc) > 300:
                self.contract_warns.append({"tool": name, "problem": "描述超长(>300)"})

    # ── 实调 ──
    def schema_of(self, name: str) -> dict:
        for t in self.tools:
            if t.get("name") == name:
                return t.get("inputSchema") or {}
        return {}

    def filter_args(self, name: str, args: dict) -> dict:
        props = (self.schema_of(name).get("properties") or {})
        return {k: v for k, v in args.items() if k in props}

    def auto_args(self, name: str) -> dict:
        schema = self.schema_of(name)
        props = schema.get("properties") or {}
        req = schema.get("required") or []
        out: dict = {}
        for r in req:
            ps = props.get(r) or {}
            t = ps.get("type", "string")
            low = r.lower()
            if t == "string":
                if any(k in low for k in ("path", "root", "dir", "folder")):
                    out[r] = ROOT
                elif "file" in low:
                    out[r] = SAMPLE_FILE
                elif any(k in low for k in ("query", "text", "content", "term", "s")):
                    out[r] = "test"
                else:
                    out[r] = ""
            elif t in ("integer", "number"):
                out[r] = 1 if "max" not in low else 3
            elif t == "boolean":
                out[r] = False
            elif t == "array":
                out[r] = []
            else:
                out[r] = {}
        return out

    def run_calls(self) -> None:
        done: set[str] = set()
        # 1) 固定参数表
        for name, args in CALL_TABLE.items():
            if name not in {t["name"] for t in self.tools}:
                continue
            done.add(name)
            r = self._call(name, self.filter_args(name, args))
            self.call_results.append({"name": name, "args_source": "table",
                                      **r})
        # 2) 自动名单 + 纯函数前缀
        for name in sorted(AUTO_CALL_NAMES):
            if name in done or name not in {t["name"] for t in self.tools}:
                continue
            done.add(name)
            r = self._call(name, self.filter_args(name, self.auto_args(name)))
            self.call_results.append({"name": name, "args_source": "auto", **r})
        for t in self.tools:
            name = t["name"]
            if name in done:
                continue
            if name.startswith(AUTO_PREFIXES) and not name.startswith(
                    CONTRACT_ONLY_PREFIXES):
                done.add(name)
                r = self._call(name, self.filter_args(name, self.auto_args(name)))
                self.call_results.append({"name": name, "args_source": "auto-prefix",
                                          **r})

    # ── 汇总 ──
    def summary(self) -> dict:
        fails = [c for c in self.call_results if not c["ok"]]
        table_fails = [c for c in fails if c.get("args_source") == "table"]
        auto_fails = [c for c in fails if c.get("args_source") != "table"]
        slow = [c for c in self.call_results if c["duration_ms"] > SLOW_MS]
        critical = [c for c in self.call_results if c["duration_ms"] > CRITICAL_MS]
        total_ms = sum(c["duration_ms"] for c in self.call_results)
        return {
            "contract": {
                "total": len(self.tools),
                "passed": len(self.tools) - len(self.contract_fails),
                "failed": len(self.contract_fails),
                "warnings": len(self.contract_warns),
            },
            "calls": {
                "total": len(self.call_results),
                "passed": len(self.call_results) - len(fails),
                "failed": len(fails),
                "table_failed": len(table_fails),
                "auto_failed": len(auto_fails),
                "total_ms": round(total_ms, 1),
                "slow_count": len(slow),
                "critical_count": len(critical),
            },
            "slow_tools": sorted(
                [{"name": c["name"], "duration_ms": round(c["duration_ms"], 1)}
                 for c in slow], key=lambda x: -x["duration_ms"])[:10],
            "failed_calls": [
                {"name": c["name"], "source": c.get("args_source"),
                 "error": c["error"]} for c in fails[:20]],
            "contract_failed": self.contract_fails[:20],
            "contract_warnings": self.contract_warns[:10],
        }

    def run(self) -> int:
        t_start = time.perf_counter()
        try:
            self.start()
            # initialize
            self.send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2024-11-05",
                                  "capabilities": {},
                                  "clientInfo": {"name": "rx-isolated-test",
                                                 "version": "0.1"}}})
            resp = self.recv()
            if resp.get("id") != 1:
                print(f"[FAIL] initialize id 不匹配: {resp}")
                return 1
            info = resp["result"].get("serverInfo", {})
            self.protocol_ok = info.get("name") == "unified-rx"
            print(f"[ok] initialize -> {info}")
            self.send({"jsonrpc": "2.0", "method": "notifications/initialized",
                       "params": {}})
            # tools/list
            self.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                       "params": {}})
            resp = self.recv()
            self.tools = resp["result"]["tools"]
            print(f"[ok] tools/list -> {len(self.tools)} 工具")
            # 契约验收（全量）
            self.contract_check()
            print(f"[契约] {len(self.tools) - len(self.contract_fails)}/"
                  f"{len(self.tools)} 通过, {len(self.contract_fails)} 失败")
            for f in self.contract_fails[:10]:
                print(f"  [契约FAIL] {f['tool']}: {f['problems']}")
            # 实调
            if not self.calls_only:
                self.run_calls()
                s = self.summary()
                print(f"[实调] {s['calls']['passed']}/{s['calls']['total']} 通过, "
                      f"总耗时 {s['calls']['total_ms']}ms")
                if s["calls"]["failed"]:
                    for f in s["failed_calls"]:
                        print(f"  [CALL FAIL] {f['name']}({f['source']}): "
                              f"{f['error'][:120]}")
                if s["slow_tools"]:
                    print("[慢工具]")
                    for x in s["slow_tools"]:
                        print(f"  {x['name']}: {x['duration_ms']}ms")
                report = {
                    "generated_at": datetime.now().isoformat(timespec="seconds"),
                    "mode": "isolated",
                    "server": self.server,
                    "protocol": {"ok": self.protocol_ok,
                                 "tools": len(self.tools)},
                    **s,
                }
                reports_dir = Path(ROOT) / "reports"
                reports_dir.mkdir(exist_ok=True)
                out = reports_dir / f"ISOLATED_TEST_{datetime.now():%Y%m%d_%H%M%S}.json"
                out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                               encoding="utf-8")
                print(f"[报告] {out}")
                elapsed = time.perf_counter() - t_start
                print(f"[耗时] {elapsed:.1f}s")
                if self.contract_fails:
                    return 1
                if s["calls"]["table_failed"]:
                    return 1
                if s["calls"]["critical_count"]:
                    print("[警告] 存在临界慢工具(>2s)——建议优化")
                print("ISOLATED TEST PASS")
                return 0
            print("CONTRACT ONLY PASS")
            return 0 if not self.contract_fails else 1
        except Exception as exc:
            print(f"[FATAL] {exc}")
            return 2
        finally:
            self.stop()


def main() -> int:
    ap = argparse.ArgumentParser(description="unified-rx 隔离验收测试")
    ap.add_argument("--calls-only", action="store_true", help="只跑代表实调")
    ap.add_argument("--json", action="store_true", help="只输出 JSON 报告路径")
    args = ap.parse_args()
    code = IsolatedTest().run()
    sys.exit(code)


if __name__ == "__main__":
    main()
