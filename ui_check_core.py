#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""Bevy UI 静态检查器（程序驱动，非 skill）——ui_check 工具核心。

检查 Bevy ECS UI 代码的常见崩溃/不可见模式：
  - ui_root_missing   : spawn UI 但无 Node 组件
  - camera_missing    : 有 UI spawn 但无 Camera（3D/2D）
  - mode_isolation    : 编辑模式/演示模式 UI 未隔离（模式切换时未隐藏）
  - focus_pass        : 全屏 Node 无 FocusPolicy::Pass（点击被吞）
  - font_missing      : Text 组件无字体兜底（中文字体缺失白屏）
  - z_ordering        : 绝对定位重叠且无层级（z_index）

纯文本扫描（正则 + 简单状态跟踪），零依赖，适配 unified-rx 契约。
"""

import re

_RULES = {
    "ui_root_missing": ("error", "spawn UI 节点但无 Node 组件（UI 不会渲染）"),
    "camera_missing": ("error", "存在 UI 但无相机（UI 不可见）"),
    "mode_isolation": ("warning", "编辑模式/演示模式 UI 未隔离（模式切换未隐藏）"),
    "focus_pass": ("warning", "全屏 Node 无 FocusPolicy::Pass（点击被 UI 吞掉）"),
    "font_missing": ("warning", "Text 组件无字体兜底（CJK 字体缺失会白屏/方框）"),
    "z_ordering": ("warning", "绝对定位重叠且无 z_index 层级（遮挡/闪烁）"),
}

# Node spawn 且带 PositionType::Absolute 的节点
_ABSOLUTE_RE = re.compile(r"PositionType::Absolute")
# FocusPolicy 设置
_FOCUS_PASS_RE = re.compile(r"FocusPolicy::Pass")
# Text 组件
_TEXT_RE = re.compile(r"Text(::|Bundle|\b)")
# 字体资源
_FONT_RE = re.compile(r"Font\(|font:|UiCjkFont|Font::default|asset_server\.load\(.*font|\.insert\(font|insert\(.*font\)|font,|font\)")
# 相机
_CAMERA_RE = re.compile(r"Camera3d|Camera2d|Camera \{\}|UiCamera")
# 编辑模式标记（编辑模式隔离的常见命名）
_EDIT_MARK_RE = re.compile(r"editor_|EditorMode|edit_mode|is_editing")
# spawn UI 节点
_SPAWN_UI_RE = re.compile(r"spawn\((?:Node|Text|Button|Image|Panel|Bar|Slot|Sprite|NodeBundle)")
# z_index
_Z_RE = re.compile(r"z_index|ZIndex")

# Bevy UI 组件标记（UI 特有的 Component 派生）
_UI_COMPONENT_RE = re.compile(r"#\[derive\(Component[^\]]*\)\]\s*\n\s*pub struct (HudRoot|.*Panel.*|.*Button.*|.*Text|.*Bar|.*Slot|.*Inventory|.*Menu)")


def scan_ui_source(src: str, path: str = "", dir_mode: bool = False) -> list[dict]:
    """扫描单个 Rust 文件，返回 issue 列表（unified-rx 契约：{rule,severity,line,msg}）。
    dir_mode=True 时跳过文件级 camera 提示（目录模式由 scan_ui_dir 聚合检查）。"""
    issues = []
    lines = src.splitlines()
    _last_z_rep = [0]  # z_ordering 去重状态：上次报告行号（for 循环内可变，用列表容器）

    # 相机存在性（文件级）
    has_camera = bool(_CAMERA_RE.search(src))
    # UI spawn 存在性
    has_ui = bool(_SPAWN_UI_RE.search(src)) or bool(_UI_COMPONENT_RE.search(src))

    for i, line in enumerate(lines, 1):
        # ui_root_missing: spawn 块里只有 Component 标记没有 Node
        if re.search(r"spawn\([^)]*\)", line) and "Node" not in line:
            # 检查接下来几行是否有 Node（spawn 多行形式）或 Node 样式函数（panel_node_style() 等）
            block = "\n".join(lines[max(0, i - 1) : i + 6])
            style_fn = re.search(r"spawn\(([a-z_]+)\(", line)
            has_node_style = bool(style_fn and re.search(r"node|style|panel|root|container", style_fn.group(1)))
            if has_node_style:
                continue  # spawn(node_style_fn()) 视为有 Node（误报修复）
            if "Node" not in block and re.search(r"\.insert\([^)]*[A-Z][a-zA-Z]+\)", block):
                # 排除纯逻辑 spawn（无 UI 标记组件）
                if _UI_COMPONENT_RE.search(block) or re.search(r"Text|Button|Panel|Bar|Slot", block):
                    issues.append({"rule": "ui_root_missing", "severity": "error",
                                   "line": i, "msg": "spawn UI 但未见 Node 组件"})

        # focus_pass: 全屏绝对定位 Node 无 FocusPolicy::Pass（块检测，兼容多行 Node 写法）
        if "PositionType::Absolute" in line:
            # 2026-08-14 补齐：等号写法（style.width = Val::Percent）+ 窗口前移
            # 3 行（width/height 常写在 position 前——Bevy style 赋值顺序任意）
            # + FocusPolicy 检查限当前节点（原实现块跨节点——第二个覆盖层的
            # FocusPolicy 会豁免第一个，同文件双覆盖层漏检）
            block = "\n".join(lines[max(0, i - 3) : i + 10])
            nxt = next((j for j in range(i + 1, min(len(lines), i + 30))
                        if "PositionType::Absolute" in lines[j]), len(lines))
            node_block = "\n".join(lines[i:nxt])
            if re.search(r"width\s*[:=]\s*Val::Percent\(100", block) \
                    and "FocusPolicy" not in node_block:
                issues.append({"rule": "focus_pass", "severity": "warning",
                               "line": i, "msg": "全屏绝对定位 Node 无 FocusPolicy::Pass（点击穿透）"})

        # z_ordering: 多个绝对定位且无 z_index（last_reported 行号去重，防重复且不丢边缘场景）
        if "PositionType::Absolute" in line:
            block = "\n".join(lines[max(0, i - 30) : i + 30])
            abs_count = len(_ABSOLUTE_RE.findall(block))
            if abs_count >= 3 and not _Z_RE.search(block):
                # 去重：30 行内已报过则跳过（相邻节点只报 1 条；跨度 31-59 行不丢失）
                if i - _last_z_rep[0] >= 30 or _last_z_rep[0] == 0:
                    _last_z_rep[0] = i
                    issues.append({"rule": "z_ordering", "severity": "warning",
                                   "line": i, "msg": "多个绝对定位节点无 z_index 层级"})

        # font_missing: Text 使用但无字体资源（行级）
        if "Text::new" in line or "Text(" in line or ".insert(Text" in line:
            block = "\n".join(lines[max(0, i - 5) : i + 10])
            if not _FONT_RE.search(block):
                issues.append({"rule": "font_missing", "severity": "warning",
                               "line": i, "msg": "Text 无字体兜底（CJK 缺失会方框/白屏）"})

        # IDE 增强 121/164：交互缺失——Button 系 spawn 无交互处理
        # （死按钮；164：支持 UiButton/TextButton/ImageButton/IconButton 变体；
        # 只查 spawn 行本身——块检查会误吃相邻按钮的 Interaction）
        if re.search(r"\bspawn\([^)]*(?:Button|Btn)", line) \
                or re.search(r"\bUi(?:Button|TextButton|ImageButton|IconButton)\b", line):
            if not re.search(r"Interaction|on_press|on_click|Pressed|Clicked|listener|"
                             r"\.clicked|pressed\s*\(|Released", line):
                issues.append({"rule": "no_interaction", "severity": "warning",
                               "line": i, "msg": "Button 无交互处理（Interaction/点击事件）——死按钮"})

    # 文件级 camera 检查：camera 在别的文件 → 单文件模式降级提示（目录模式在 scan_ui_dir 聚合）
    if not dir_mode and has_ui and not has_camera:
        issues.append({"rule": "camera_missing", "severity": "warning",
                       "line": 0, "msg": "文件含 UI 但未见相机（单文件模式——建议用目录扫描确认）"})

    # mode_isolation: 编辑模式标记存在但 UI 组件未在模式检查中隐藏（加 has_ui 门控防纯逻辑文件误报）
    if has_ui and _EDIT_MARK_RE.search(src) and not re.search(r"Hidden|Visible|visibility|despawn|toggle", src):
        issues.append({"rule": "mode_isolation", "severity": "warning",
                       "line": 0, "msg": "编辑模式标记存在但未见 UI 显隐逻辑（模式隔离缺失）"})

    return issues


def _scan_cs_ui(src: str, path: str) -> list[dict]:
    """Unity（.cs）UI 检查（IDE 增强 267：Button 创建无 onClick 连接——
    死按钮；对齐 Bevy/Godot）。窗口 = 创建行 + 后 2 行。"""
    import re as _re
    issues = []
    _btn = _re.compile(r"\bButton\b")
    _click = _re.compile(r"onClick|on_click|AddListener|\.onClick\.AddListener|"
                         r"clicked\s*=|Click\s*\+=")
    lines = src.splitlines()
    for i, line in enumerate(lines, 1):
        if not _btn.search(line):
            continue
        if "using " in line and "Button" not in line.split("using ")[-1]:
            continue  # import 行非按钮创建
        block = "\n".join(lines[i - 1:i + 2])
        if not _click.search(block):
            issues.append({
                "rule": "no_interaction", "severity": "warning", "line": i,
                "msg": "Button 无点击处理（onClick.AddListener 缺失）——死按钮",
                "file": path})
    return issues


def _scan_gd_ui(src: str, path: str) -> list[dict]:
    """Godot（.gd）UI 检查（IDE 增强 257：用户点名"没有多语言处理 包括扫描"——
    Bevy 之外的游戏 UI 同样检查）。

    规则：Button/TextureButton 创建后无 pressed 连接（死按钮——
    对齐 Bevy no_interaction）。块窗口 8 行（spawn 后找连接）。"""
    import re as _re
    issues = []
    _btn = _re.compile(r"\b(?:Button|TextureButton)\.new\(\)|"
                       r"add_child\([^)]*[Bb]utton\)")
    _pressed = _re.compile(r"pressed\.connect|_pressed\b|\.pressed\s*=|"
                           r"connect\(\s*[\"']pressed")
    lines = src.splitlines()
    for i, line in enumerate(lines, 1):
        if not _btn.search(line):
            continue
        # 窗口 = 创建行 + 后 2 行（slice [i-1:i+2]——3 行；Godot 常见
        # `var b = Button.new()` 下一行 connect；宽窗口会把后续按钮的
        # 连接误算进来"救活"死按钮）
        block = "\n".join(lines[i - 1:i + 2])
        if not _pressed.search(block):
            issues.append({
                "rule": "no_interaction", "severity": "warning", "line": i,
                "msg": "Button 无按下处理（pressed.connect 缺失）——死按钮",
                "file": path})
    return issues


def scan_ui_dir(root: str, max_files: int = 100) -> list[dict]:
    """扫描目录下 .rs（Bevy）与 .gd（Godot）文件（限 max_files）；
    聚合检查相机存在性（目录级）。"""
    import os
    issues = []
    files = []
    gd_files = []
    any_ui = False
    any_camera = False
    for r, _, names in os.walk(root):
        for n in sorted(names):
            if n.endswith(".rs"):
                files.append(os.path.join(r, n))
            elif n.endswith(".gd"):
                gd_files.append(os.path.join(r, n))
            elif n.endswith(".cs"):
                # IDE 增强 267：Unity（.cs）UI 文件
                gd_files.append(os.path.join(r, n))  # 复用 gd 收集桶（下方按扩展分发）
            if len(files) + len(gd_files) >= max_files:
                break
        if len(files) + len(gd_files) >= max_files:
            break
    # Godot/Unity UI 规则（.gd/.cs——Bevy 规则不适用）
    for f in gd_files:
        try:
            size = os.path.getsize(f)
            if size > (1 << 20):
                continue
            with open(f, encoding="utf-8", errors="replace") as fh:
                gsrc = fh.read()
        except OSError:
            continue
        if f.endswith(".cs"):
            for iss in _scan_cs_ui(gsrc, f):
                issues.append(iss)
        else:
            for iss in _scan_gd_ui(gsrc, f):
                issues.append(iss)
    for f in files:
        try:
            size = os.path.getsize(f)
            if size > (1 << 20):
                issues.append({"file": f, "rule": "file_too_large", "severity": "warning",
                               "line": 0, "msg": "文件过大跳过"})
                continue
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                src = fh.read()
        except OSError as exc:
            issues.append({"file": f, "rule": "read_error", "severity": "warning",
                           "line": 0, "msg": f"读取失败: {exc}"})
            continue
        if _SPAWN_UI_RE.search(src) or _UI_COMPONENT_RE.search(src):
            any_ui = True
        if _CAMERA_RE.search(src):
            any_camera = True
        for issue in scan_ui_source(src, f, dir_mode=True):
            issue["file"] = f
            issues.append(issue)
    # 目录级相机检查（跨文件聚合，防单文件误报）
    if any_ui and not any_camera:
        issues.append({"file": root, "rule": "camera_missing", "severity": "error",
                       "line": 0, "msg": "目录含 UI 但未见相机（UI 不可见）"})
    return issues
