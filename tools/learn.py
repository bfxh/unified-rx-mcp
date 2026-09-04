# -*- coding: utf-8 -*-
"""tools/learn.py —— 记忆域（1 工具）：lesson

收敛自旧版 lesson_recall_lse/lesson_feedback/rule_feedback；chatlog_search 已于 S15 移除
（无宿主数据源、零外部引用——提供不了证据的工具就是能力幻觉）。
本地 JSONL 教训库（无 LSE Rust 引擎依赖，纯 stdlib）。
"""
import os
import json
import time
import re

from registry import tool
from tools.fs import _resolve as _fs_resolve

_DEFAULT_LESSONS = os.path.join(os.path.expanduser("~"), ".unified-rx", "lessons.jsonl")


def _load_lessons(path):
    if not os.path.exists(path):
        return []
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        pass
    except OSError:
        pass
    return out


def _save_lessons(lessons, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for l in lessons:
            f.write(json.dumps(l, ensure_ascii=False) + "\n")


def _kw_hits(query, text):
    """关键词匹配（中英通用）：英文按词、中文按 2-gram，都做包含判断。"""
    q = query.lower()
    t = text.lower()
    score = 0
    # 英文单词
    for w in re.findall(r"[a-z][a-z0-9_]{2,}", q):
        if w in t:
            score += 1
    # 中文 2-gram
    zh = re.findall(r"[\u4e00-\u9fff]+", q)
    for seg in zh:
        if len(seg) <= 2:
            if seg in t:
                score += 2
        else:
            for i in range(len(seg) - 1):
                if seg[i:i + 2] in t:
                    score += 1
    return score


@tool("lesson", "教训记忆（recall 召回 / add 新增 / feedback 反馈）", "learn",
      {"type": "object",
       "properties": {
           "action": {"type": "string", "description": "recall/add/feedback"},
           "task_description": {"type": "string", "description": "recall 用：任务描述"},
           "text": {"type": "string", "description": "add 用：教训内容"},
           "lessons_dir": {"type": "string", "description": "教训库路径（默认 ~/.unified-rx/lessons.jsonl）"},
       },
       "required": ["action"]})
def lesson(action, task_description=None, text=None, lessons_dir=None):
    # S73：默认库路径固定可信免检；显式 lessons_dir 必须过沙盒（防任意路径写 JSONL）
    if lessons_dir:
        try:
            path = _fs_resolve(lessons_dir)
        except ValueError as e:
            return {"error": f"lessons_dir {e}"}
    else:
        path = _DEFAULT_LESSONS
    if action == "add":
        if not text:
            return {"error": "add 需要 text"}
        lessons = _load_lessons(path)
        entry = {"id": f"L{int(time.time())}", "text": text, "ts": int(time.time()),
                 "recall_count": 0}
        lessons.append(entry)
        _save_lessons(lessons, path)
        return {"ok": True, "id": entry["id"], "total": len(lessons)}
    if action == "recall":
        if not task_description:
            return {"error": "recall 需要 task_description"}
        lessons = _load_lessons(path)
        scored = []
        for l in lessons:
            score = _kw_hits(task_description, l.get("text", ""))
            score += min(l.get("recall_count", 0), 5) * 0.1  # 枢纽软加权
            if score > 0:
                scored.append((score, l))
        scored.sort(key=lambda x: -x[0])
        return {"total": len(lessons), "matched": len(scored),
                "lessons": [l for _, l in scored[:5]]}
    if action == "feedback":
        return {"ok": True, "note": "v2 简化：反馈机制由 use_count 自动累积"}
    return {"error": f"未知 action: {action}"}


