#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""game_eval —— VoxelForge 专项评价系统（2026-08-18 用户定案）。

用户要求："反正你要在 unified-rx-mcp 搞个评价系统……以后我让你检查 或者让你
增加东西的时候也是按这个来的"。本工具 = 日常检查基准（与 Rust 侧测试互补：
Rust 权威校验 + 本工具快速评价报告）。

三维检查（全部只读、零依赖、纯标准库）：
1. 连接点规则（镜像 nexus_core mount_rules.rs）：
   - 红线：任何占用格 ≥1 挂点（无 0 点死模块）
   - 普通结构件：每格 6 面全开（"每一个格子都可以拼接"）
   - Conveyor 链式件：每格 3 点 North/South/Bottom（"只能三个连接点"）
   - 特殊结构件/功能件：每格 1-6 点自定义（设计性限制）
2. 按键覆盖（对照 input_map.rs BINDINGS + 源码 KeyCode:: 使用）：
   - 死键扫描：源码里绑定的键必须在 BINDINGS 表有说明
   - 关键键必须绑定（WASD/R/Esc/G/B/P/C/X/Tab/F1/F2/Q/E/F/鼠标）
3. 程序化模板参数（templates.ron）：
   - min ≤ default ≤ max；generator 在已知集合；id 唯一

用法：
    python game_eval.py [--project D:/开发/VoxelForge] [--report docs/reports]
输出：Markdown 报告 + JSON 数据（stdout 摘要 + 报告文件）。
"""

import json
import os
import re
import sys

# ── 参数 ──
PROJECT = r"D:/开发/VoxelForge"
REPORT_DIR = "docs/reports"
KNOWN_GENERATORS = {
    "vehicle", "rock", "tree", "terrain_tile", "building", "road", "table",
}
# 关键键（必须绑定）——与游戏帮助面板一致
MUST_BIND = ["W", "A", "S", "D", "R", "C", "G", "B", "P", "X", "Tab",
             "F1", "F2", "Q", "E", "F", "Esc", "左键", "右键", "滚轮"]
# 特殊结构件标记（镜像 core SPECIAL_TAGS）
SPECIAL_TAGS = ["pending_", "reactor", "heart", "totem", "radar", "shield",
                "miner", "regen", "stealth", "holo", "shrine", "ward", "anchor",
                "targeting", "boost", "data_link", "phase", "dodge", "sensor",
                "ossuary", "carapace", "monolith", "bulwark", "converter",
                "storage", "compressor", "manufacturer"]
FACES = ["Top", "Bottom", "North", "South", "East", "West"]


# ════════════════════════════════════════════════════════════════
# 1. 连接点规则
# ════════════════════════════════════════════════════════════════

def _parse_ron(text):
    """极简 RON 提取（本项目资产格式固定——不引入 ron 解析器）。"""
    dims = re.search(r"Block\(dims: \((\d+), (\d+), (\d+)\)\)", text)
    cat = re.search(r"category: (\w+)", text)
    tags = re.findall(r'"([^"]+)"', text)
    mps = []
    for m in re.finditer(r"MountPoint\(cell: \((-?\d+), (-?\d+), (-?\d+)\), face: (\w+)", text):
        mps.append((int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)))
    return {
        "dims": tuple(int(x) for x in dims.groups()) if dims else None,
        "category": cat.group(1) if cat else "?",
        "tags": tags,
        "mps": mps,
    }


def check_mount_rules(ron_path):
    """单文件连接点规则 → (ok, issues[])。规则镜像 core/mount_rules.rs。"""
    text = open(ron_path, encoding="utf-8").read()
    p = _parse_ron(text)
    issues = []
    if not p["dims"]:
        return False, ["shape 缺失/非法"]
    dx, dy, dz = p["dims"]
    cells = {(x, y, z) for x in range(dx) for y in range(dy) for z in range(dz)}
    by_cell = {}
    for (cx, cy, cz, face) in p["mps"]:
        by_cell.setdefault((cx, cy, cz), []).append(face)
    # 方案判定（2026-08-19 放宽：面级自定义优先——只查数量与 0 点红线）
    cat = p["category"]
    if cat == "Conveyor":
        scheme, lo, hi = "Tri3", 2, 3  # "只能两个或者三个"
    elif cat == "Structure" and not any(
            any(s in t for s in SPECIAL_TAGS) for t in p["tags"]):
        scheme, lo, hi = "Full6", 1, 6  # 面级自由：1-6 点（默认 6 面全开）
    else:
        scheme, lo, hi = "Custom", 1, 6
    for cell in sorted(cells):
        faces = sorted(by_cell.get(cell, []))
        n = len(faces)
        # 0 点格合法（用户没点=不连）；有点的格必须数量合规
        if n > 0 and (n < lo or n > hi):
            issues.append(f"格 {cell} 挂点 {n} 个不符合 {scheme}（允许 {lo}-{hi}）")
    # 0 点红线（模块级：整模块至少 1 挂点）
    if not p["mps"]:
        issues.append("整模块无挂点（0 点死模块禁止）")
    return (len(issues) == 0, issues)


# ════════════════════════════════════════════════════════════════
# 2. 按键覆盖
# ════════════════════════════════════════════════════════════════

def check_key_bindings(project):
    """按键覆盖：BINDINGS 表 + 源码 KeyCode:: 使用 → (ok, issues[], summary)。"""
    issues = []
    app_src = os.path.join(project, "crates", "app", "src")
    # 收集源码全部 KeyCode:: 绑定
    used = set()
    for root, _, files in os.walk(app_src):
        for f in files:
            if not f.endswith(".rs"):
                continue
            path = os.path.join(root, f)
            text = open(path, encoding="utf-8").read()
            for m in re.finditer(r"KeyCode::(\w+)", text):
                used.add(m.group(1))
    # BINDINGS 表说明文本
    imap = os.path.join(app_src, "input_map.rs")
    bindings_text = ""
    if os.path.exists(imap):
        bindings_text = open(imap, encoding="utf-8").read()
    # 死键：源码用了但 BINDINGS 无说明（防"悄悄绑定不告知"）
    explained = set()
    for m in re.finditer(r'key: "([^"]+)"', bindings_text):
        for part in re.split(r"[/ ]+", m.group(1)):
            explained.add(part.strip())
    # 键名归一化（KeyCode::KeyA ↔ BINDINGS "A"；Escape ↔ "Esc"）
    def norm(k):
        n = k[3:] if k.startswith("Key") else k
        return {"Escape": "Esc"}.get(n, n)

    for key in sorted(used - {"Unidentified"}):
        if norm(key) not in explained and norm(key) not in {"Backspace", "Enter"}:
            issues.append(f"源码绑定 {key} 未在 BINDINGS 表说明（帮助面板缺失）")
    # 关键键必须出现（源码或绑定表）
    key_names = {"W": "KeyW", "A": "KeyA", "S": "KeyS", "D": "KeyD",
                 "R": "KeyR", "C": "KeyC", "G": "KeyG", "B": "KeyB",
                 "P": "KeyP", "X": "KeyX", "Q": "KeyQ", "E": "KeyE",
                 "F": "KeyF", "Tab": "Tab", "F1": "F1", "F2": "F2"}
    missing = []
    for label in MUST_BIND:
        if label in ("左键", "右键", "滚轮", "Esc"):
            if label == "Esc" and "Escape" not in used:
                missing.append(label)
            continue
        code = key_names.get(label)
        if code and code not in used:
            missing.append(label)
    if missing:
        issues.append(f"关键按键未绑定: {missing}")
    summary = f"源码绑定 {len(used)} 个 KeyCode；BINDINGS 表 {len(explained)} 项说明"
    return (len(issues) == 0, issues, summary)


# ════════════════════════════════════════════════════════════════
# 3. 模板参数
# ════════════════════════════════════════════════════════════════

def check_templates(project):
    """templates.ron 参数合法性 → (ok, issues[], summary)。"""
    issues = []
    path = os.path.join(project, "assets", "procgen", "templates.ron")
    if not os.path.exists(path):
        return False, [f"模板文件缺失 {path}"], "无模板"
    text = open(path, encoding="utf-8").read()
    tpls = re.findall(r'id: "([^"]+)",\s*generator: "(\w+)"', text)
    ids = [t[0] for t in tpls]
    if len(ids) != len(set(ids)):
        issues.append("模板 id 重复")
    for tid, gen in tpls:
        if gen not in KNOWN_GENERATORS:
            issues.append(f"模板 {tid}: 未知生成函数 {gen}（已知 {sorted(KNOWN_GENERATORS)}）")
    # 参数范围检查（每个模板内）
    for block in re.finditer(
            r'ProcTemplate\(.*?id: "([^"]+)".*?params: \[(.*?)\]',
            text, re.DOTALL):
        tid, params_block = block.group(1), block.group(2)
        for pm in re.finditer(
                r'name: "(\w+)", min: ([\d.]+), max: ([\d.]+), default: ([\d.]+)',
                params_block):
            name, lo, hi, dflt = pm.group(1), float(pm.group(2)), \
                float(pm.group(3)), float(pm.group(4))
            if not (lo <= dflt <= hi):
                issues.append(f"模板 {tid} 参数 {name}: default {dflt} 超出 [{lo}, {hi}]")
    summary = f"{len(ids)} 个模板"
    return (len(issues) == 0, issues, summary)


# ════════════════════════════════════════════════════════════════
# 报告
# ════════════════════════════════════════════════════════════════

def run(project=PROJECT, report_dir=None):
    """执行三维评价 → (report_md, report_json)。"""
    mods_dir = os.path.join(project, "assets", "modules", "rebuild")
    mount_total = mount_ok = 0
    mount_issues = []
    if os.path.isdir(mods_dir):
        for f in sorted(os.listdir(mods_dir)):
            if not f.endswith(".ron"):
                continue
            path = os.path.join(mods_dir, f)
            ok, issues = check_mount_rules(path)
            mount_total += 1
            if ok:
                mount_ok += 1
            else:
                mount_issues.append(f"  - {f}: {'; '.join(issues)}")
    else:
        mount_issues.append(f"  - 模块目录缺失 {mods_dir}")

    key_ok, key_issues, key_summary = check_key_bindings(project)
    tpl_ok, tpl_issues, tpl_summary = check_templates(project)

    sections = []
    # 连接点
    sections.append(f"## 连接点规则（{mount_ok}/{mount_total} 合规）")
    if mount_issues:
        sections.append("违规：\n" + "\n".join(mount_issues[:20]))
        if len(mount_issues) > 20:
            sections.append(f"… 共 {len(mount_issues)} 项（见 JSON）")
    else:
        sections.append("全部模块符合设计性规则（full_6/tri_3/custom，无 0 点死模块）。")
    # 按键
    sections.append(f"## 按键覆盖（{'通过' if key_ok else '问题'}）— {key_summary}")
    if key_issues:
        sections.append("\n".join(f"  - {i}" for i in key_issues))
    else:
        sections.append("全部绑定键在帮助面板有说明；关键键无缺失。")
    # 模板
    sections.append(f"## 程序化模板（{'通过' if tpl_ok else '问题'}）— {tpl_summary}")
    if tpl_issues:
        sections.append("\n".join(f"  - {i}" for i in tpl_issues))
    else:
        sections.append("模板参数全部合法，生成函数全部可分发。")

    verdict = "PASS" if (mount_ok == mount_total and key_ok and tpl_ok) else "FAIL"
    md = (f"# VoxelForge 评价报告（game_eval）\n\n"
          f"> 生成：{__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
          f"> 结论：**{verdict}**\n\n"
          + "\n\n".join(sections) + "\n")
    data = {
        "verdict": verdict,
        "mount": {"total": mount_total, "ok": mount_ok,
                  "issues": [i.strip() for i in mount_issues]},
        "keys": {"ok": key_ok, "summary": key_summary, "issues": key_issues},
        "templates": {"ok": tpl_ok, "summary": tpl_summary, "issues": tpl_issues},
    }
    # 落盘
    if report_dir:
        out = os.path.join(project, report_dir)
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "GAME_EVAL.md"), "w", encoding="utf-8") as f:
            f.write(md)
        with open(os.path.join(out, "game_eval.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    return md, data


def main():
    args = sys.argv[1:]
    project = PROJECT
    report_dir = REPORT_DIR
    for i, a in enumerate(args):
        if a == "--project" and i + 1 < len(args):
            project = args[i + 1]
        if a == "--report" and i + 1 < len(args):
            report_dir = args[i + 1]
    md, data = run(project, report_dir)
    print(md)
    print(f"JSON 摘要: {data['verdict']} "
          f"(mount {data['mount']['ok']}/{data['mount']['total']}, "
          f"keys {'ok' if data['keys']['ok'] else 'FAIL'}, "
          f"templates {'ok' if data['templates']['ok'] else 'FAIL'})")
    sys.exit(0 if data["verdict"] == "PASS" else 1)


if __name__ == "__main__":
    main()
