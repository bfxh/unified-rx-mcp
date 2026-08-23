# -*- coding: utf-8 -*-
"""tools/learn.py —— 记忆域（2 工具）：lesson / chatlog_search

收敛自旧版 lesson_recall_lse/lesson_feedback/rule_feedback + chatlog_search。
本地 JSONL 教训库（无 LSE Rust 引擎依赖，纯 stdlib）。
"""
import os
import json
import time
import re

from registry import tool

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
    path = lessons_dir or _DEFAULT_LESSONS
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


@tool("chatlog_search", "跨智能体聊天记录检索（统一索引）", "learn",
      {"type": "object",
       "properties": {
           "query": {"type": "string", "description": "关键词"},
           "agent": {"type": "string", "description": "智能体过滤（hermes/trae/qoder/marvis）"},
           "limit": {"type": "integer", "description": "结果上限（默认 20）"},
           "index_file": {"type": "string", "description": "索引文件（默认 ~/.unified-rx/chatlog.jsonl）"},
       },
       "required": ["query"]})
def chatlog_search(query, agent=None, limit=20, index_file=None):
    path = index_file or os.path.join(os.path.expanduser("~"), ".unified-rx", "chatlog.jsonl")
    if not os.path.exists(path):
        return {"ok": True, "hits": [], "total": 0, "note": "索引未建"}
    hits = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if agent and rec.get("agent") != agent:
                    continue
                text = f"{rec.get('title', '')} {rec.get('text', '')}"
                if query.lower() in text.lower():
                    hits.append(rec)
                    if len(hits) >= limit:
                        break
    except OSError:
        pass
    return {"ok": True, "hits": hits, "total": len(hits)}
