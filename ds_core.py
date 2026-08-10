#!/usr/bin/env python3
"""设计系统认知层（design system awareness）——AI 懂的 UI 规范：
1. design_tokens.json（W3C Design Tokens 格式）——单一事实源
2. ds_lookup：查 token 值（AI 生成 UI 时引用正确 token）
3. ds_check：验证 Rust UI 代码是否符合 tokens（硬编码值检出/规则合规）

AI 引流：AI 写 UI 前调 ds_lookup 拿 token，写完后调 ds_check 验证——设计系统被程序消费。
"""

import json
import os
import re

_TOKENS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "design_tokens.json")

# Rust 代码中的硬编码 UI 值 → token 对照（检测偏离）
_HARDCODED_PATTERNS = {
    "font_size": re.compile(r"font_size:\s*([\d.]+)"),
    "padding": re.compile(r"(?:left|right|top|bottom):\s*Val::Px\(([\d.]+)\)"),
    "gap": re.compile(r"(?:column_gap|row_gap):\s*Val::Px\(([\d.]+)\)"),
    "radius": re.compile(r"border_radius:.*?Val::Px\(([\d.]+)\)"),
}

# 允许的维度值：从 tokens 动态派生（新增 dimension token 自动纳入，防误报，review should-fix）
def _allowed_dimensions() -> set[float]:
    flat = lookup_tokens().get("tokens", {})
    allowed = set()
    for info in flat.values():
        val = info.get("value", "")
        if info.get("type") == "dimension" and isinstance(val, str) and val.endswith("px"):
            try:
                allowed.add(float(val[:-2]))
            except ValueError:
                continue
    return allowed


def _load_tokens() -> dict:
    with open(_TOKENS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def lookup_tokens(path: str = "") -> dict:
    """返回 tokens 摘要（组 + token 名 + 值），AI 可直接引用。"""
    tokens = _load_tokens()
    flat = {}

    def walk(node, prefix):
        for k, v in node.items():
            if isinstance(v, dict) and "$value" in v:
                val = v["$value"]
                if isinstance(val, dict) and "value" in val:
                    val = f"{val['value']}{val.get('unit', '')}"
                elif isinstance(val, dict) and "components" in val:
                    comps = ",".join(str(round(c, 3)) for c in val["components"])
                    alpha = val.get("alpha", 1.0)
                    val = f"rgba({comps}, {alpha})"
                flat[f"{prefix}.{k}"] = {"type": v.get("$type", ""), "value": val}
            elif isinstance(v, dict):
                walk(v, f"{prefix}.{k}" if prefix else k)

    walk(tokens, "")
    return {"ok": True, "token_count": len(flat), "tokens": flat}


def check_ui_code(src: str, path: str = "") -> list[dict]:
    """检查 Rust UI 代码是否符合设计系统：
    1. 硬编码维度值偏离 tokens → violation
    2. 规则合规（字体兜底/模式隔离标记）
    """
    issues = []
    lines = src.splitlines()
    allowed = _allowed_dimensions()  # 动态派生（review should-fix：新增 token 自动纳入）

    # 1. 硬编码值偏离
    for i, line in enumerate(lines, 1):
        for kind, pat in _HARDCODED_PATTERNS.items():
            for m in pat.finditer(line):
                try:
                    val = float(m.group(1))
                except ValueError:
                    continue
                if val not in allowed:
                    issues.append({
                        "rule": "hardcoded_value",
                        "severity": "warning",
                        "line": i,
                        "msg": f"硬编码 {kind}={val}px 不在 design tokens 中（允许: {sorted(allowed)}）",
                        "file": path,
                    })

    # 2. 字体兜底：Text 使用但无 UiCjkFont 引用（收紧到精确 token 引用，review should-fix）
    has_font_ref = "UiCjkFont" in src
    for i, line in enumerate(lines, 1):
        if ("Text::new" in line or "Text(" in line) and not has_font_ref:
            issues.append({
                "rule": "font_fallback_missing",
                "severity": "error",
                "line": i,
                "msg": "Text 未使用 UiCjkFont token（CJK 缺失白屏）",
                "file": path,
            })

    # 3. 模式隔离：编辑模式标记 + HUD 组件无 Hidden
    if "EditorMode" in src or "is_editing" in src:
        has_hidden = "Visibility::Hidden" in src or "Hidden" in src
        has_hud = "HudRoot" in src or "VehicleNameText" in src or "MiniMap" in src
        if has_hud and not has_hidden:
            issues.append({
                "rule": "mode_isolation_violation",
                "severity": "error",
                "line": 0,
                "msg": "存在编辑模式与 HUD 组件但无 Visibility::Hidden（模式隔离 token 未应用）",
                "file": path,
            })

    return issues


def check_directory(root: str, max_files: int = 200) -> dict:
    """目录级设计系统检查（聚合所有 .rs）。"""
    issues = []
    files = []
    for r, _, names in os.walk(root):
        for n in sorted(names):
            if n.endswith(".rs"):
                files.append(os.path.join(r, n))
                if len(files) >= max_files:
                    break
        if len(files) >= max_files:
            break
    for f in files:
        try:
            if os.path.getsize(f) > (1 << 20):
                continue
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                src = fh.read()
        except OSError:
            continue
        issues.extend(check_ui_code(src, os.path.relpath(f, root)))
    return {"ok": True, "file_count": len(files), "issue_count": len(issues), "issues": issues}
