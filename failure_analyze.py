#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""failure_analyze —— 根因分析（RCA，阶段2，TraceStation/test_report_analyzer 式）。

输入 traceback/失败文本 → 输出根因链报告：
  ① 解析：异常类型/消息/文件:行 帧链（纯正则，坏输入宽容）
  ② 关联（证据收集）：
     - telemetry：最近工具错误（同错误消息/同时间段）
     - scan-log：该 root 最近扫描已知问题
     - git：最近提交（谁改了什么——文件命中则高置信）
     - alarms：最近告警（卡死/慢/错误率）
  ③ 候选根因：规则驱动打分排序（证据强度）
  ④ 建议：下一步动作（causal_trace/bug_bisect/定向测试）

全部只读——不自动改代码。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time


def _parse_traceback(text: str) -> dict:
    """从 traceback 文本提取：异常消息 / 文件:行 帧链。坏输入宽容。"""
    lines = [l.rstrip() for l in str(text).splitlines()]
    exc = ""
    frames: list[dict] = []
    for line in lines:
        m = re.search(r'File "([^"]+)", line (\d+)', line)
        if m:
            frames.append({"file": m.group(1), "line": int(m.group(2))})
    for line in reversed(lines):
        if line.strip():
            exc = line.strip()[:200]
            break
    return {
        "exception": exc,
        "frames": frames[:10],
        "file": frames[0]["file"] if frames else "",
        "line": frames[0]["line"] if frames else None,
    }


def _git_recent(root: str, n: int = 10) -> list[dict]:
    """最近提交（git log，只读；非 git 项目返回空）。"""
    try:
        r = subprocess.run(
            ["git", "-C", root, "log", "--format=%H|%an|%ad|%s",
             "--date=iso", f"-{n}"],
            capture_output=True, text=True, timeout=15, encoding="utf-8",
            errors="replace")
        out = []
        if r.returncode == 0:
            for line in r.stdout.strip().splitlines():
                parts = line.split("|", 3)
                if len(parts) == 4:
                    out.append({"hash": parts[0][:10], "author": parts[1],
                                "time": parts[2], "message": parts[3][:100]})
        return out
    except (OSError, subprocess.TimeoutExpired):
        return []


def _git_touched_file(root: str, file_path: str, n: int = 20) -> list[dict]:
    """最近改动过该文件的提交（git log -- <file>）。"""
    try:
        r = subprocess.run(
            ["git", "-C", root, "log", "--format=%H|%an|%ad|%s",
             "--date=iso", f"-{n}", "--", file_path],
            capture_output=True, text=True, timeout=15, encoding="utf-8",
            errors="replace")
        out = []
        if r.returncode == 0:
            for line in r.stdout.strip().splitlines():
                parts = line.split("|", 3)
                if len(parts) == 4:
                    out.append({"hash": parts[0][:10], "author": parts[1],
                                "time": parts[2], "message": parts[3][:100]})
        return out
    except (OSError, subprocess.TimeoutExpired):
        return []


def _scan_log_for(root: str, n: int = 20) -> list[dict]:
    """scan-log 中该 root 最近扫描记录（已知问题）。"""
    try:
        import scan_log_core
        logs = scan_log_core.query_logs(limit=200)
        out = []
        for l in logs:
            if root and root not in str(l.get("root", "")):
                continue
            if l.get("ok") is False or l.get("summary"):
                out.append({"ts": l.get("ts", 0), "tool": l.get("tool", ""),
                            "ok": l.get("ok"), "summary": str(l.get("summary", ""))[:100]})
        return out[-n:]
    except Exception:  # noqa: BLE001 —— 尽力而为
        return []


def failure_analyze(text: str, root: str = "", limit: int = 200) -> dict:
    """根因分析主入口：traceback → 根因链报告。"""
    tb = _parse_traceback(text)
    evidence: list[dict] = []
    candidates: list[dict] = []

    # ── 证据 1：遥测最近错误（同错误消息匹配优先） ──────────────
    try:
        import telemetry_core
        errs = telemetry_core.recent_errors(10)
        if errs:
            matched = []
            for e in errs:
                if tb["exception"] and tb["exception"][:40] in str(e.get("err", "")):
                    matched.append(e)
            evidence.append({"source": "telemetry_recent_errors",
                             "matched": matched, "items": errs[:5]})
            if matched:
                candidates.append({
                    "rank": 1, "confidence": "high",
                    "hypothesis": "同一错误近期在工具调用中反复出现（遥测记录）",
                    "evidence": [f"tool={m.get('tool')} err={m.get('err', '')[:60]}"
                                 for m in matched[:3]]})
    except Exception:  # noqa: BLE001
        pass

    # ── 证据 2：git 最近提交 + 异常文件命中 ─────────────────────
    if root and os.path.isdir(root):
        commits = _git_recent(root, 10)
        if commits:
            evidence.append({"source": "git_recent", "items": commits[:5]})
        if tb["file"]:
            touched = _git_touched_file(root, tb["file"], 10)
            if touched:
                evidence.append({"source": "git_touched_file",
                                 "file": tb["file"], "items": touched[:3]})
                candidates.append({
                    "rank": 1, "confidence": "high",
                    "hypothesis": f"异常文件 {tb['file']} 最近被提交改动——"
                                  f"新变更引入回归的可能性最高",
                    "evidence": [f"{c['hash']} {c['message']}" for c in touched[:3]]})
            elif commits:
                candidates.append({
                    "rank": 2, "confidence": "medium",
                    "hypothesis": "异常文件近期无改动，根因可能在依赖/调用方/环境",
                    "evidence": [f"文件未出现在最近 {10} 条提交中"]})
    else:
        commits = []
        if tb["file"]:
            candidates.append({
                "rank": 2, "confidence": "medium",
                "hypothesis": "未提供 root——无法关联 git 提交，建议传入项目路径",
                "evidence": []})

    # ── 证据 3：scan-log 已知问题 ──────────────────────────────
    if root:
        scan_issues = _scan_log_for(root, 10)
        if scan_issues:
            evidence.append({"source": "scan_log", "items": scan_issues[:5]})
            candidates.append({
                "rank": 3, "confidence": "medium",
                "hypothesis": "scan-log 显示该项目近期扫描出问题",
                "evidence": [f"{s.get('tool')}: {s.get('summary')}" for s in scan_issues[:3]]})

    # ── 证据 4：告警 ───────────────────────────────────────────
    try:
        import telemetry_core
        alarms = telemetry_core.read_alarms(10)
        if alarms:
            evidence.append({"source": "alarms", "items": alarms[:5]})
            crit = [a for a in alarms if a.get("level") == "CRITICAL"]
            if crit:
                candidates.append({
                    "rank": 3, "confidence": "medium",
                    "hypothesis": "存在 CRITICAL 告警（daemon 卡死/系统异常）可能为间接根因",
                    "evidence": [f"{a.get('rule')}: {a.get('msg', '')[:60]}"
                                 for a in crit[:2]]})
    except Exception:  # noqa: BLE001
        pass

    # ── 文件存在性验证（防幻觉：引用前先验证） ─────────────────
    file_exists = False
    if tb["file"]:
        cand = tb["file"]
        if os.path.isabs(cand):
            file_exists = os.path.exists(cand)
        elif root:
            file_exists = os.path.exists(os.path.join(root, cand.lstrip("/\\")))
    if tb["file"] and not file_exists:
        candidates.append({
            "rank": 4, "confidence": "low",
            "hypothesis": "异常文件不存在于本地——可能已删除/重命名或路径来自其他机器",
            "evidence": [f"{tb['file']} 不存在"]})

    # ── 建议（规则） ──────────────────────────────────────────
    suggestions = []
    if root:
        suggestions.append(f"causal_trace(root={root}) 回溯行为因果链")
        suggestions.append("bug_bisect 二分定位引入提交（good_commit → bad_commit）")
    suggestions.append("telemetry_query(status=error) 查看同窗口其他失败")
    suggestions.append("修复后跑对应测试 + std_check 验证")
    if not tb["frames"]:
        suggestions.append("输入不是标准 traceback（无 File 行）——可粘贴原始报错文本")

    return {
        "ok": True,
        "analyzed_at": time.time(),
        "exception": tb["exception"] or "(未识别到异常消息)",
        "location": {"file": tb["file"], "line": tb["line"],
                     "exists": file_exists,
                     "frames": tb["frames"]},
        "evidence": evidence,
        "candidates": sorted(candidates, key=lambda c: c["rank"])[:5],
        "suggestions": suggestions,
        "hint": "根因链按证据强度排序；rank=1 优先验证",
    }


if __name__ == "__main__":  # CLI 调试入口
    import sys
    text = sys.argv[1] if len(sys.argv) > 1 else "Traceback: File \"x.py\", line 3, in main\nValueError: boom"
    root = sys.argv[2] if len(sys.argv) > 2 else ""
    print(json.dumps(failure_analyze(text, root), ensure_ascii=False, indent=1))
