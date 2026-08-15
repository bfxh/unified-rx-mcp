#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""causal_debug —— 因果建模与调试（2026-08-15，阶段1）。

不再是找"哪里错了"，而是问"为什么错"：
① causal_trace：事件因果链——scan-log 调用记录 + git 提交 → 失败事件
  → 溯源到引入它的 Agent 行为/工具调用（"是哪个行为导致构建失败"）
② bug_bisect：git bisect 式二分——自动化二分查找引入 bug 的提交
  （真实 git 操作——只读：log/rev-list/checkout 由调用方确认）

全部只读/建议层——不自动改代码。
"""
import json
import os
import re
import subprocess


# ── ① 因果溯源 ─────────────────────────────────────────────
def causal_trace(root: str, fail_keyword: str = "fail",
                 limit: int = 200) -> dict:
    """因果溯源：失败事件 → 回溯因果链（最近的代码变更 + 工具调用）。

    数据源：
    - git log（最近提交——代码变更因果）
    - scan-log（工具调用记录——Agent 行为因果）
    输出：候选原因链（按时间倒序）——"先看哪个变更/行为"
    """
    chain: list[dict] = []
    # A. git 提交因果（最近 20 条——含作者/消息/时间）
    try:
        r = subprocess.run(
            ["git", "-C", root, "log", "--format=%H|%an|%ad|%s",
             "--date=iso", "-20"],
            capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            for line in r.stdout.strip().splitlines():
                parts = line.split("|", 3)
                if len(parts) == 4:
                    chain.append({"kind": "commit", "hash": parts[0][:10],
                                  "author": parts[1], "time": parts[2],
                                  "message": parts[3][:100]})
    except (OSError, subprocess.TimeoutExpired):
        pass  # 非 git 项目/超时——跳过提交因果
    # B. scan-log 工具调用因果（最近调用——Agent 行为）
    try:
        import scan_log_core
        logs = scan_log_core.query_logs(limit=limit)
        for l in logs[-30:]:
            chain.append({"kind": "tool_call",
                          "tool": l.get("tool", ""),
                          "root": str(l.get("root", ""))[:60],
                          "summary": str(l.get("summary", ""))[:100],
                          "time": l.get("ts", "")})
    except Exception:  # 尽力而为
        pass
    # C. 失败事件定位（最近失败记录）
    fails = []
    try:
        import scan_log_core
        for l in scan_log_core.query_logs(limit=limit):
            sm = str(l.get("summary", ""))
            if fail_keyword.lower() in sm.lower() or l.get("ok") is False:
                fails.append({"tool": l.get("tool", ""),
                              "summary": sm[:100],
                              "time": l.get("ts", "")})
    except Exception:  # 尽力而为
        pass
    # 因果结论：失败前最近的变更/行为（倒序链前 10 条）
    return {"ok": True, "root": root, "fail_keyword": fail_keyword,
            "fail_events": fails[:10],
            "causal_chain": chain[:15],
            "advice": ("因果溯源：失败事件发生前最近的提交/工具调用是首要嫌疑"
                       "（链首）——用 bug_bisect 二分确认引入提交；"
                       "用 predict_impact 预测修复影响面")}


# ── ② git 二分定位 ─────────────────────────────────────────
def bug_bisect(root: str, good_commit: str, bad_commit: str,
               test_cmd: str, max_steps: int = 15,
               execute: bool = False) -> dict:
    """git bisect 式二分：在 [good, bad] 区间二分查找引入 bug 的提交。

    execute=False（默认）：只读计划（rev-list 计数 + mid 建议——不 checkout）。
    execute=True：实际执行二分（checkout mid 提交 → 跑 test_cmd → 收缩区间）
    ——写操作（checkout）受 L4 授权（调用方显式确认）。
    实现用 git bisect 原生命令（start/bad/good/run——不手写二分循环）。
    """
    import subprocess as _sp
    # execute 路径：git bisect 原生（start bad good → run test_cmd）
    if execute:
        try:
            # 重置可能的旧 bisect 状态
            _sp.run(["git", "-C", root, "bisect", "reset"],
                    capture_output=True, text=True, timeout=15)
            r = _sp.run(["git", "-C", root, "bisect", "start",
                         bad_commit, good_commit],
                        capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                return {"ok": False, "error": f"bisect start 失败: {r.stderr[:120]}"}
            try:
                r = _sp.run(["git", "-C", root, "bisect", "run",
                             *test_cmd.split()],
                            capture_output=True, text=True, timeout=600)
                out = (r.stdout or "") + (r.stderr or "")
                first_bad = _extract_first_bad(out)
            finally:
                # 安全（security-review MEDIUM）：无论结果/异常都恢复 HEAD
                # ——防超时/异常后工作区停在 mid 提交 + BISECT 状态残留
                _sp.run(["git", "-C", root, "bisect", "reset"],
                        capture_output=True, text=True, timeout=15)
            return {"ok": True, "executed": True,
                    "first_bad_commit": first_bad,
                    "log_tail": out[-500:],
                    "advice": f"引入 bug 的提交: {first_bad or '未定位（测试命令退出码语义检查）'}"
                              "——修复后 causal_trace 溯源行为链"}
        except (OSError, _sp.TimeoutExpired) as e:
            return {"ok": False, "error": f"bisect 执行失败: {e}"}
    # 只读计划路径（原行为）
    try:
        r = subprocess.run(
            ["git", "-C", root, "rev-list", "--count", f"{good_commit}..{bad_commit}"],
            capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return {"ok": False, "error": f"rev-list 失败: {r.stderr[:100]}"}
        total = int(r.stdout.strip())
        if total <= 0:
            return {"ok": False, "error": "区间无提交（good/bad 顺序或范围错误）"}
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        return {"ok": False, "error": f"git 不可用: {e}"}
    # 二分计划（不实际 checkout——建议层）
    mid = total // 2
    plan = (f"区间 {good_commit[:8]}..{bad_commit[:8]} 共 {total} 个提交——"
            f"二分第 1 步：checkout 第 {mid} 个提交（约一半）跑 {test_cmd[:40]}，"
            f"按结果收缩区间——最多 {max_steps} 步定位引入提交")
    return {"ok": True, "total_commits": total, "mid_index": mid,
            "max_steps": min(max_steps, total.bit_length()),
            "next": plan,
            "advice": "加 execute=true 实际执行 git bisect（L4 授权——会 checkout）；"
                      "确认定位后：修复提交 + causal_trace 溯源行为链"}


def _extract_first_bad(output: str) -> str | None:
    """从 git bisect run 输出提取 'first bad commit' 的 hash。"""
    m = re.search(r"first bad commit:\s*\[?([0-9a-f]{7,40})", output)
    if m:
        return m.group(1)
    return None


# ── ③ 因果链记录（scan-log 扩展）──────────────────────────
def record_cause(root: str, effect: str, cause: str) -> dict:
    """记录因果链（cause → effect——scan-log tool=causal_link）。

    供 Agent 行为链回放：哪个行为（cause）导致了什么结果（effect）。
    """
    try:
        import scan_log_core
        scan_log_core.append_scan({
            "tool": "causal_link", "root": root, "ok": True,
            "summary": f"因果: {cause[:60]} → {effect[:60]}"})
        return {"ok": True, "cause": cause, "effect": effect,
                "log": "因果链已入 scan-log（tool=causal_link 可查）"}
    except Exception as e:  # 尽力而为
        return {"ok": False, "error": str(e)[:80]}
