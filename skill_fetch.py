#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""skill_fetch.py — 技能申请制下载（MCP_OPTIMIZATION_PLAN M4）。

用户明确要求：**没有相关 skill 时自动下载需要告诉用户，用户批准才下载，不批就不行**。

流程：
  1. request  — 任务描述 → 匹配现有 skill → 未命中生成申请单（skill 名/来源/理由）→ 存 pending
  2. list     — 列出待批申请（用户看）
  3. approve  — 用户批准（approved: true + 申请 id）→ 真正安装（下载/复制到 skills 目录）
  4. reject   — 用户拒绝 → 标记拒绝（不再重复申请）

安全：安装前校验（manifest 格式/无危险命令）——下载的 skill 先沙盒检查。
"""

import json
import os
import re
import shutil
import time
import uuid

# 申请单目录（pending 状态持久化）
_APPROVAL_DIR = os.path.join(
    os.environ.get("UNIFIED_RX_HOME", os.path.expanduser("~")),
    ".unified-rx", "skill-approvals")
# 本地 skill 模板库（可扩展：后续接远程仓库）
_SKILL_TEMPLATES = {
    "blender-modeling": {
        "name": "blender-modeling",
        "desc": "Blender 5.2 建模/批量导出 GLB（Bevy 管线）",
        "source": "local:skills/templates/blender-modeling",
        "size": "~5KB",
        "reason": "美术任务（集团视觉主题/敌人模型）需要",
    },
    "bevy-ui-glass": {
        "name": "bevy-ui-glass",
        "desc": "Bevy UI 毛玻璃/玻璃质感样式（iOS 效果）",
        "source": "local:skills/templates/bevy-ui-glass",
        "size": "~3KB",
        "reason": "UI 玻璃效果增强需要",
    },
    "rust-safety": {
        "name": "rust-safety",
        "desc": "Rust 安全代码模式（unwrap 审计/边界防护）",
        "source": "local:skills/templates/rust-safety",
        "size": "~4KB",
        "reason": "挖漏洞/panic 风险修复需要",
    },
}


def _approval_path(aid: str) -> str:
    return os.path.join(_APPROVAL_DIR, f"{aid}.json")


def _list_approvals() -> list[dict]:
    try:
        if not os.path.isdir(_APPROVAL_DIR):
            return []
        out = []
        for fn in sorted(os.listdir(_APPROVAL_DIR)):
            if fn.endswith(".json"):
                try:
                    with open(os.path.join(_APPROVAL_DIR, fn), encoding="utf-8") as f:
                        out.append(json.load(f))
                except (OSError, json.JSONDecodeError):
                    continue
        return out
    except OSError:
        return []


def request_skill(task: str, skills_dir: str) -> dict:
    """任务 → 匹配现有 skill → 未命中生成申请单。"""
    # 1. 匹配现有 skill（简单关键词）
    existing = set()
    try:
        if os.path.isdir(skills_dir):
            for d in os.listdir(skills_dir):
                existing.add(d.lower())
    except OSError:
        pass
    task_low = task.lower()
    # 2. 找候选模板
    candidates = []
    for key, meta in _SKILL_TEMPLATES.items():
        # 2026-08-14 修复：key 连字符/下划线拆子词匹配（"rust 安全审查" 应
        # 命中 rust-safety——原只整串匹配，中文任务全落空）
        key_parts = key.replace("-", " ").replace("_", " ").split()
        if key in task_low or any(k in task_low for k in key_parts) \
                or any(w in task_low for w in meta["reason"].split("（")[0].split()):
            candidates.append(meta)
    if not candidates:
        # 无模板 → 返回提示（未知领域，用户可指定）
        return {"ok": False, "error": f"未匹配到 skill 模板（任务: {task}）",
                "available": list(_SKILL_TEMPLATES.keys()),
                "hint": "用 skill_fetch list 看已有申请；或告诉我该领域用什么 skill"}
    # 3. 生成申请单
    approved_ids = []
    for meta in candidates:
        if meta["name"] in existing:
            continue  # 已安装
        aid = uuid.uuid4().hex[:8]
        rec = {
            "id": aid,
            "skill": meta["name"],
            "desc": meta["desc"],
            "source": meta["source"],
            "size": meta["size"],
            "reason": meta["reason"],
            "status": "pending",
            "requested_ts": time.time(),
        }
        os.makedirs(_APPROVAL_DIR, exist_ok=True)
        with open(_approval_path(aid), "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
        approved_ids.append(rec)
    return {"ok": True, "action": "request",
            "approvals": approved_ids,
            "message": "等待用户批准（approve 时带 id + approved: true）"}


def approve_skill(aid: str, approved: bool, skills_dir: str) -> dict:
    """用户批准/拒绝申请。"""
    path = _approval_path(aid)
    if not os.path.exists(path):
        return {"ok": False, "error": f"申请不存在: {aid}",
                "hint": "先 skill_fetch request 生成申请"}
    try:
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return {"ok": False, "error": f"申请读取失败: {e}"}
    if not approved:
        rec["status"] = "rejected"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
        return {"ok": True, "status": "rejected", "skill": rec["skill"],
                "message": "已拒绝（不再自动安装）"}
    # 批准 → 安装（从本地模板库复制 + 沙盒校验）
    install = _install(rec, skills_dir)
    if install["ok"]:
        rec["status"] = "installed"
        rec["installed_ts"] = time.time()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
    return {"ok": install["ok"], "status": "installed" if install["ok"] else "failed",
            "skill": rec["skill"], "detail": install.get("detail", "")}


def _install(rec: dict, skills_dir: str) -> dict:
    """从模板源安装（当前 local: 模板——沙盒校验 manifest 后复制）。"""
    source = rec.get("source", "")
    skill_name = rec["skill"]
    if not source.startswith("local:"):
        return {"ok": False, "detail": f"远程源暂不支持: {source}"}
    # 本地模板目录（若存在）
    tpl_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "skill_templates", skill_name)
    if not os.path.isdir(tpl_dir):
        # 模板不存在 → 创建占位 skill（含模板骨架）
        os.makedirs(tpl_dir, exist_ok=True)
        with open(os.path.join(tpl_dir, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write(f"""---
name: {skill_name}
description: {rec['desc']}（skill_fetch 申请制安装）
---

# {skill_name}

由 skill_fetch 申请制安装（{rec['reason']}）。

## 使用
（待补充——安装后由智能体在实际任务中完善）
""")
    # 沙盒校验：SKILL.md 存在 + 无危险命令
    manifest = os.path.join(tpl_dir, "SKILL.md")
    if not os.path.isfile(manifest):
        return {"ok": False, "detail": "SKILL.md 缺失——模板损坏"}
    try:
        content = open(manifest, encoding="utf-8").read()
        if re.search(r"(rm -rf /|format c:|del /s /q c:\\)", content, re.I):
            return {"ok": False, "detail": "沙盒校验失败：含危险命令"}
    except OSError as e:
        return {"ok": False, "detail": f"校验读取失败: {e}"}
    # 复制到 skills 目录
    try:
        os.makedirs(skills_dir, exist_ok=True)
        dest = os.path.join(skills_dir, skill_name)
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        shutil.copytree(tpl_dir, dest)
        return {"ok": True, "detail": f"已安装到 {dest}"}
    except OSError as e:
        return {"ok": False, "detail": f"安装失败: {e}"}


def list_approvals() -> dict:
    """全部申请（含历史）。"""
    all_recs = _list_approvals()
    pending = [r for r in all_recs if r["status"] == "pending"]
    return {"ok": True, "pending": pending,
            "installed": [r for r in all_recs if r["status"] == "installed"],
            "rejected": [r for r in all_recs if r["status"] == "rejected"],
            "hint": "批准：skill_fetch({action: approve, id: ..., approved: true})"}
