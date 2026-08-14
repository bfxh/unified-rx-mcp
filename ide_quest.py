#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""ide_quest.py — Quest 任务状态机（IDE_ENHANCE_PLAN R7，抄 Qoder Quest）。

"修这个 bug" → 状态链：诊断 → 定位 → 影响面 → 修复建议 → 验证 → 教训。
可断点续跑：每步结果落盘（JSON 状态文件），中断后从最后完成步继续。
"""

import json
import os
import re
import time

STEPS = [
    ("diagnose", "诊断：bug_scan/quality_scan 找问题"),
    ("locate", "定位：locate_edit 找具体位置"),
    ("impact", "影响面：change_impact 评估改动波及"),
    ("fix", "修复建议：ide_actions/ide_rename 生成修改"),
    ("verify", "验证：lsp_query 复查 + 回归"),
    ("lesson", "教训：lesson_recall 记录经验"),
]

# 任务目录（状态文件）
_QUEST_DIR = os.environ.get(
    "UNIFIED_RX_QUEST_DIR",
    str(os.path.join(os.path.expanduser("~"), ".unified-rx", "quests")))


class Quest:
    """单任务状态机。"""

    def __init__(self, quest_id: str, task: str, repo: str):
        self.quest_id = quest_id
        self.task = task
        self.repo = repo
        self.state: dict = {
            "quest_id": quest_id,
            "task": task,
            "repo": repo,
            "created_ts": time.time(),
            "current_step": 0,
            "steps": {name: {"done": False, "result": None} for name, _ in STEPS},
            "finished": False,
        }

    @classmethod
    def load(cls, quest_id: str) -> "Quest | None":
        # IDE 增强 168（安全）：load 同样校验（防读取目录外 json——`../x`）
        if not re.fullmatch(r"[A-Za-z0-9_-]+", quest_id):
            return None
        path = cls._state_path(quest_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        q = cls(quest_id, state.get("task", ""), state.get("repo", ""))
        q.state = state
        return q

    @staticmethod
    def _state_path(quest_id: str) -> str:
        return os.path.join(_QUEST_DIR, f"{quest_id}.json")

    def current_step_name(self) -> str:
        if self.state["finished"]:
            return "done"
        idx = self.state["current_step"]
        if idx >= len(STEPS):
            return "done"
        return STEPS[idx][0]

    def complete_step(self, result: dict) -> dict:
        """完成当前步：存结果 → 前进。返回下一步信息。"""
        if self.state.get("aborted"):
            # 2026-08-14 修复：abort 后不可继续（实测 abort 后 step 仍
            # ok=True 前进——中止语义失效）
            return {"ok": False, "error": "任务已中止（aborted）——不可继续"}
        if self.state["finished"]:
            return {"ok": False, "error": "任务已完成"}
        idx = self.state["current_step"]
        if idx >= len(STEPS):
            self.state["finished"] = True
            self._save()
            return {"ok": True, "finished": True}
        name, _desc = STEPS[idx]
        self.state["steps"][name] = {"done": True, "result": result, "ts": time.time()}
        self.state["current_step"] = idx + 1
        if idx + 1 >= len(STEPS):
            self.state["finished"] = True
        self._save()
        return {
            "ok": True,
            "completed": name,
            "next": self.current_step_name(),
            "finished": self.state["finished"],
        }

    def status(self) -> dict:
        done = [n for n, _ in STEPS if self.state["steps"][n]["done"]]
        return {
            "quest_id": self.quest_id,
            "task": self.task,
            "current": self.current_step_name(),
            "done_steps": done,
            "finished": self.state["finished"],
            "aborted": self.state.get("aborted", False),
            "elapsed_s": round(time.time() - self.state.get("created_ts", time.time())),
            "notes_count": len(self.state.get("notes", [])),
        }

    def abort(self) -> dict:
        """放弃任务（保留状态供复盘——不删除状态文件）。"""
        if self.state.get("aborted"):
            return {"ok": False, "error": "任务已放弃"}
        self.state["aborted"] = True
        self.state["aborted_ts"] = time.time()
        self._save()
        return {"ok": True, "quest_id": self.quest_id, "aborted": True}

    def add_note(self, text: str) -> dict:
        """任务备注（断点续跑上下文——记下发现/思路，中途不丢）。"""
        if not text or not text.strip():
            return {"ok": False, "error": "备注为空"}
        notes = self.state.setdefault("notes", [])
        notes.append({"ts": time.time(), "text": text.strip()})
        self._save()
        return {"ok": True, "quest_id": self.quest_id,
                "notes": notes, "notes_count": len(notes)}

    def _save(self) -> None:
        try:
            os.makedirs(_QUEST_DIR, exist_ok=True)
            with open(self._state_path(self.quest_id), "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
        except OSError:  # 尽力而为（吞错可追溯）
            pass


def new_quest(quest_id: str, task: str, repo: str) -> Quest:
    # IDE 增强 168（安全）：quest_id 仅允许安全字符（防路径注入——
    # quest_id 直接进文件名，`../evil` 会写出 _QUEST_DIR）
    if not re.fullmatch(r"[A-Za-z0-9_-]+", quest_id):
        raise ValueError(f"quest_id 非法: {quest_id}（仅允许 [A-Za-z0-9_-]）")
    return Quest(quest_id, task, repo)


def resume_quest(quest_id: str) -> Quest | None:
    return Quest.load(quest_id)


def list_quests() -> list[dict]:
    """列出全部任务（状态摘要）。"""
    try:
        if not os.path.isdir(_QUEST_DIR):
            return []
        out = []
        for fn in sorted(os.listdir(_QUEST_DIR)):
            if not fn.endswith(".json"):
                continue
            q = Quest.load(fn[:-5])
            if q:
                out.append(q.status())
        # IDE 增强 167：活跃任务计数（进行中/未中止——AI 一眼看到手头任务）
        _active = [s for s in out
                   if not s.get("finished") and not s.get("aborted")]
        return [{"active_count": len(_active), "total": len(out), "quests": out}]
    except OSError:
        return []


def build_auto_report(ctx: dict) -> str:
    """自动诊断 markdown 报告（2026-08-15 从 server _tool_ide_quest 拆出——
    纯字符串构造，CC=164 巨型处理器瘦身第一刀）。

    ctx 键：args/path/quest_id/mode/chain/result_note/scan_data/loc/
    std_sev/result_verdict/chain_elapsed/log_path/notes_count
    """
    import time as _t
    args = ctx.get("args") or {}
    path = ctx.get("path", "")
    quest_id = ctx.get("quest_id", "")
    mode = ctx.get("mode", "full")
    chain = ctx.get("chain") or []
    result_note = ctx.get("result_note", "")
    scan_data = ctx.get("scan_data") or {}
    loc = ctx.get("loc") or {}
    std_sev = ctx.get("std_sev") or {}
    result_verdict = ctx.get("result_verdict", "failed")
    chain_elapsed = ctx.get("chain_elapsed", 0)
    log_path = ctx.get("log_path", "~/.unified-rx/scan-log.jsonl")
    notes_count = ctx.get("notes_count", 0)

    _step_titles = {"diagnose": "诊断", "locate": "定位", "impact": "影响面",
                    "fix": "修复建议", "verify": "验证", "lesson": "教训"}
    _report = [f"# 自动诊断报告",
               f"**任务名**：{str(args.get('task', ''))[:60] or '（未命名）'}",
               f"**结果**：{'✅ success' if result_verdict == 'success' else '⚠️ partial' if result_verdict == 'partial' else '❌ failed'}",
               f"**模式**：{'⚡ quick（未深查影响面）' if mode == 'quick' else 'full'}",
               f"**时间**：{_t.strftime('%Y-%m-%d %H:%M:%S')}",
               f"**路径**：`{path}`", f"**耗时**：{chain_elapsed}s",
               f"**文件**：{scan_data.get('files', '?')}",
               f"**扫描耗时**：{next((c.get('elapsed_s') for c in chain if c.get('tool') == 'bug_scan'), '?')}s",
               f"**定位**：`{loc.get('file', '')}:{loc.get('line', 0)}`"
               f" [{loc.get('rule', '')}]" if loc.get("file") else "",
               *(["**重跑**：force（覆盖上次链）"] if args.get("force") else []),
               f"**任务**：`{quest_id}`",
               f"**结论**：{result_note[:100]}",
               f"**步数**：{len(chain)}（含 std_check 联动）" if any(
                   c.get("tool") == "std_check" for c in chain) else f"**步数**：{len(chain)}",
               f"**重现**：`ide_quest action=auto path={path}`",
               f"**验证**：修复后 `ide_quest action=verify_fix quest_id={quest_id}`",
               f"**日志**：`{log_path}`（`scan_log root={path}` 查询）",
               f"**配置**：mode={mode} / max_files={args.get('max_files', '默认')} / "
               f"limit={args.get('limit', '默认')}",
               f"**扫描**：bug_scan error {scan_data.get('severity_counts', {}).get('error', 0)}"
               f" / std Critical {std_sev.get('Critical', 0)}"
               f" Error {std_sev.get('Error', 0)}"
               f" Warning {std_sev.get('Warning', 0)}", ""]
    for c in chain:
        _report.append(f"### {_step_titles.get(c.get('step', ''), c.get('step', ''))}")
        _report.append(c.get("summary", ""))
        _report.append("")
        if c.get("elapsed_s") is not None:
            _report.append(f"⏱ {c.get('elapsed_s')}s")
            _report.append("")
    _report.append("### 耗时分布")
    _report.append("| 步骤 | 耗时 |")
    _report.append("|---|---|")
    for c in chain:
        _report.append(f"| {_step_titles.get(c.get('step', ''), c.get('step', ''))} "
                       f"| {c.get('elapsed_s', 0)}s |")
    _report.append("")
    _report.append("> 后续：`stats_summary` 统计 / `vuln_scan` 三引擎复扫 / "
                   "`pipeline preset=audit_repo` 一键审计；扫描文件被修改后 "
                   "shadow 扫描自动补扫。")
    _report.append(f"> 任务备注：{notes_count} 条"
                   f"（`ide_quest action=status quest_id={quest_id}` 查看）。")
    return "\n".join(_report)
