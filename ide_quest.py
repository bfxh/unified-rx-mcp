#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""ide_quest.py — Quest 任务状态机（IDE_ENHANCE_PLAN R7，抄 Qoder Quest）。

"修这个 bug" → 状态链：诊断 → 定位 → 影响面 → 修复建议 → 验证 → 教训。
可断点续跑：每步结果落盘（JSON 状态文件），中断后从最后完成步继续。
"""

import json
import os
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
_QUEST_DIR = os.environ.get("UNIFIED_RX_QUEST_DIR", ".unified-rx-quests")


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
        }

    def _save(self) -> None:
        try:
            os.makedirs(_QUEST_DIR, exist_ok=True)
            with open(self._state_path(self.quest_id), "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
        except OSError:
            pass


def new_quest(quest_id: str, task: str, repo: str) -> Quest:
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
        return out
    except OSError:
        return []
