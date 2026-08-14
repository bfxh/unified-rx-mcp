#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""design_notes.py — 项目本质三分元数据（MCP_OPTIMIZATION_PLAN M5）。

用户方法论：
  settled   — 设定性架构：追求原样/本质（如剧情骨架/集团解锁时序）——**不改**
  adjustable— 设计性问题：多选项可调（如经济数值/trigger 机制）——**可调**
  doubts    — 疑点：新技术可能有 bug（lua 钩子边界/敌人 AI）——**先标记再验证**

存储：<root>/.unified-rx/design.json
工具：design_note {action: add/list/get, root, kind, text}
"""

import json
import os
import time

_FILENAME = "design.json"
_DIR = ".unified-rx"

KINDS = ("settled", "adjustable", "doubts")


def _path(root: str) -> str:
    return os.path.join(root, _DIR, _FILENAME)


def _load(root: str) -> dict:
    p = _path(root)
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {k: [] for k in KINDS}


def _save(root: str, data: dict) -> None:
    os.makedirs(os.path.dirname(_path(root)), exist_ok=True)
    with open(_path(root), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def add_note(root: str, kind: str, text: str, tag: str = "") -> dict:
    if kind not in KINDS:
        return {"ok": False, "error": f"kind 需在 {KINDS}，收到 {kind}"}
    if not root or not os.path.isdir(root):
        return {"ok": False, "error": f"目录不存在: {root}"}
    data = _load(root)
    rec = {"text": text, "ts": time.time()}
    if tag:
        rec["tag"] = tag
    data[kind].append(rec)
    _save(root, data)
    return {"ok": True, "kind": kind, "note": rec,
            "counts": {k: len(v) for k, v in data.items()}}


def list_notes(root: str) -> dict:
    if not root or not os.path.isdir(root):
        return {"ok": False, "error": f"目录不存在: {root}"}
    data = _load(root)
    # IDE 增强 141：最近笔记（全部笔记按 ts 降序取 3——AI 一眼看到最新决策）
    _all = [{"kind": k, **n} for k, notes in data.items() for n in notes]
    _recent = sorted(_all, key=lambda n: n.get("ts", 0), reverse=True)[:3]
    return {"ok": True, "root": root,
            "settled": data.get("settled", []),
            "adjustable": data.get("adjustable", []),
            "doubts": data.get("doubts", []),
            "counts": {k: len(v) for k, v in data.items()},
            "recent": _recent,
            # IDE 增强 149：未决疑点提示（doubts 待验证——AI 处理时注意）
            "advice": (f"有 {len(data.get('doubts', []))} 条未决疑点（doubts）——"
                       f"实现前先验证，避免按未定设定开发"
                       if data.get("doubts") else "无未决疑点"),
            # IDE 增强 152：标签统计（tag 分布——按主题聚合决策）
            "tag_counts": dict(sorted(
                {t: sum(1 for k, notes in data.items()
                        for n in notes if n.get("tag") == t)
                 for t in {n.get("tag") for k, notes in data.items()
                           for n in notes if n.get("tag")}}.items(),
                key=lambda kv: -kv[1])),
            "legend": {"settled": "设定性——原样不改",
                       "adjustable": "设计性——可调选项",
                       "doubts": "疑点——先标记再验证"}}


def get_note(root: str, kind: str) -> dict:
    if kind not in KINDS:
        return {"ok": False, "error": f"kind 需在 {KINDS}"}
    data = _load(root)
    return {"ok": True, "kind": kind, "notes": data.get(kind, [])}
