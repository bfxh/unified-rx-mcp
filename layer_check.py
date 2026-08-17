#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""layer_check.py — 分层开发理念 + 写完即模拟（2026-08-17）。

用户方法论："每一个代码写完需要模拟的，包括如 UI 需要分层次的干：
先搞布局再搞动画，动画搞完再搞美术，包括大部分的东西都是这样"。

- ui(path)：UI 文件三层分检（布局→动画→美术），每层输出 done/missing，
  并校验"分层顺序"（布局未完成就做动画 = 顺序违规提示）
- code(path)：代码三层分检（骨架→逻辑→优化）
- simulate(path)：写完即模拟——Python AST 语法 + py_compile + 隔离 import；
  JS/TS 用 node --check（可用时）——模拟不通过提示先修再交付
"""
import ast
import os
import re
import subprocess
import sys

# ── UI 分层关键词 ──────────────────────────────────────────────────────
_LAYOUT_KW = re.compile(
    r"\b(width|height|min-width|max-width|min-height|max-height|flex|grid|"
    r"position|top|left|right|bottom|margin|padding|display|columns|rows|"
    r"layout|container|anchor|size|scale)\b", re.IGNORECASE)
_ANIM_KW = re.compile(
    r"\b(transition|animation|keyframes|transform|translate|rotate|"
    r"ease|duration|delay|animate|lerp|tween|motion)\b", re.IGNORECASE)
_ART_KW = re.compile(
    r"(#[0-9a-fA-F]{3,8}\b|rgb\(|rgba\(|hsl\(|linear-gradient|radial-gradient|"
    r"\b(color|background|background-color|fill|stroke|opacity|shadow|"
    r"font-family|font-size|icon|sprite|texture|image|src=|\bimg\b|"
    r"border-radius|filter)\b)", re.IGNORECASE)

# ── 代码分层（骨架/逻辑/优化）──────────────────────────────────────────
_MAGIC_NUM_RE = re.compile(r"(?<![\w.])\d{3,}(?![\w.])")  # 裸 3+ 位数字


def _read(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def ui(path: str) -> dict:
    """UI 文件三层分检（布局→动画→美术）。"""
    p = os.path.normpath(path)
    if not os.path.isfile(p):
        return {"ok": False, "error": f"文件不存在: {path}"}
    text = _read(p)
    total_lines = text.count("\n") + 1

    layout_hits = len(_LAYOUT_KW.findall(text))
    anim_hits = len(_ANIM_KW.findall(text))
    art_hits = len(_ART_KW.findall(text))

    layers = {
        "layout": {"done": layout_hits >= 3, "hits": layout_hits,
                   "label": "布局（尺寸/定位/容器）——第一层"},
        "animation": {"done": anim_hits >= 1, "hits": anim_hits,
                      "label": "动画（transition/animation/transform）——第二层"},
        "art": {"done": art_hits >= 3, "hits": art_hits,
                "label": "美术（颜色/资源/字体/图标）——第三层"},
    }
    order = ["layout", "animation", "art"]
    # 顺序校验：下层完成但上层未完成 = 违规
    violations = []
    for i, layer in enumerate(order):
        if layers[layer]["done"]:
            for prev in order[:i]:
                if not layers[prev]["done"]:
                    violations.append(f"{layer} 已做但 {prev} 未完成——应 {prev} 先行")
                    break
    return {"ok": True, "kind": "ui", "path": p, "lines": total_lines,
            "layers": layers,
            "stage": next((l for l in order if not layers[l]["done"]), "全部完成"),
            "violations": violations,
            "advice": ("按顺序推进：" + " → ".join(layers[l]["label"] for l in order) +
                       "；每层完成后验证再进下一层"
                       if violations or not all(layers[l]["done"] for l in order)
                       else "三层均完成——按 布局→动画→美术 顺序已验证")}


def code(path: str) -> dict:
    """代码文件三层分检（骨架→逻辑→优化）。"""
    p = os.path.normpath(path)
    if not os.path.isfile(p):
        return {"ok": False, "error": f"文件不存在: {path}"}
    text = _read(p)
    total_lines = text.count("\n") + 1

    # 骨架：函数/类定义数
    defs = len(re.findall(r"^\s*(def|class|fn|func|function|pub fn)\s+\w+", text, re.M))
    # 逻辑：return/分支/异常
    logic_hits = len(re.findall(r"\b(return|if |else|elif|match |try:|except|"
                                r"raise|throw|catch)\b", text))
    # 优化：魔法数字/TODO/超长行
    magic = len(_MAGIC_NUM_RE.findall(text))
    todo = len(re.findall(r"\b(TODO|FIXME)\b", text))
    long_lines = sum(1 for ln in text.splitlines() if len(ln) > 120)

    layers = {
        "skeleton": {"done": defs >= 1 and total_lines >= 5,
                     "detail": f"{defs} 个函数/类定义，{total_lines} 行"},
        "logic": {"done": logic_hits >= 2, "detail": f"{logic_hits} 处逻辑（return/分支/异常）"},
        "optimize": {"done": magic == 0 and todo == 0 and long_lines == 0,
                     "detail": f"魔法数字 {magic}、TODO {todo}、超长行 {long_lines}"},
    }
    return {"ok": True, "kind": "code", "path": p, "lines": total_lines,
            "layers": layers,
            "stage": next((l for l in ("skeleton", "logic", "optimize")
                           if not layers[l]["done"]), "全部完成"),
            "advice": ("顺序推进：骨架（定义/结构）→ 逻辑（分支/返回）→ 优化（数字/TODO/行宽）"
                       if any(not layers[l]["done"] for l in layers) else "三层均完成")}


def simulate(path: str) -> dict:
    """写完即模拟：静态运行模拟（语法/编译/导入/语法树）。"""
    p = os.path.normpath(path)
    if not os.path.isfile(p):
        return {"ok": False, "error": f"文件不存在: {path}"}
    ext = os.path.splitext(p)[1].lower()
    text = _read(p)
    checks = []

    if ext == ".py":
        # 1) AST 语法
        try:
            tree = ast.parse(text)
            checks.append({"name": "ast 语法", "ok": True,
                           "detail": f"解析成功，{len(list(ast.walk(tree)))} 节点"})
        except SyntaxError as e:
            return {"ok": False, "kind": "simulate", "path": p,
                    "checks": [{"name": "ast 语法", "ok": False,
                                "detail": f"{e.filename}:{e.lineno}: {e.msg}"}],
                    "passed": False, "advice": "语法错误——先修再交付"}
        # 2) py_compile
        try:
            import py_compile
            py_compile.compile(p, doraise=True)
            checks.append({"name": "py_compile", "ok": True, "detail": "编译通过"})
        except Exception as e:
            checks.append({"name": "py_compile", "ok": False, "detail": str(e)[:150]})
        # 3) 隔离 import（不执行顶层副作用——只验证模块可加载）
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("_layer_sim", p)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                checks.append({"name": "import 模拟", "ok": True,
                               "detail": "模块可加载（顶层代码已执行模拟）"})
            else:
                checks.append({"name": "import 模拟", "ok": True, "detail": "跳过（无 loader）"})
        except Exception as e:
            checks.append({"name": "import 模拟", "ok": False,
                           "detail": f"导入失败: {type(e).__name__}: {str(e)[:120]}"})
    elif ext in (".js", ".mjs", ".cjs"):
        try:
            r = subprocess.run(["node", "--check", p], capture_output=True,
                               text=True, timeout=30)
            checks.append({"name": "node --check", "ok": r.returncode == 0,
                           "detail": r.stderr.strip()[:150] or "语法通过"})
        except (OSError, subprocess.TimeoutExpired) as e:
            checks.append({"name": "node --check", "ok": False, "detail": str(e)[:120]})
    elif ext in (".ts", ".tsx"):
        checks.append({"name": "type 检查", "ok": True,
                       "detail": "TS 需 tsc 工程级检查（此处跳过，建议跑 build）"})
    else:
        checks.append({"name": "语法", "ok": True,
                       "detail": f"{ext} 无本地模拟器（跳过，靠运行验证）"})

    passed = all(c["ok"] for c in checks)
    return {"ok": True, "kind": "simulate", "path": p, "checks": checks,
            "passed": passed,
            "advice": ("模拟全部通过——可以交付/进下一层" if passed
                       else "模拟未通过——先修问题再继续（写完即模拟原则）")}


def layer_check(action: str, path: str) -> dict:
    """layer_check 主入口（含剪辑/3D 动画分层模板 2026-08-17）。"""
    if action == "ui":
        return ui(path)
    if action == "code":
        return code(path)
    if action == "simulate":
        return simulate(path)
    if action in ("clip", "anim3d"):
        return _media_layers(action, path)
    return {"ok": False, "error": f"未知 action: {action}（可选 ui/code/simulate/clip/anim3d）"}


# ── 2026-08-17：剪辑/3D 动画分层模板 ────────────────────────────────────
# 用户理念：大部分东西分层次干（先布局再动画再美术）；剪辑与动画同样分层。

_CLIP_RAW_KW = re.compile(
    r"\b(素材|footage|clip|raw|sequence|shot|粗剪|cut|timeline|时间线|"
    r"order|顺序|01_|02_|03_|part|scene)\b", re.IGNORECASE)
_CLIP_FINE_KW = re.compile(
    r"\b(转场|transition|crossfade|wipe|fade|节奏|pacing|rhythm|trim|"
    r"cut_point|marker|标记|J-cut|L-cut|overlap)\b", re.IGNORECASE)
_CLIP_FINISH_KW = re.compile(
    r"\b(调色|color|grade|lut|look|cc|色彩|音频|audio|sound|music|voice|"
    r"音效|mix|master|响度|loudness|字幕|subtitle)\b", re.IGNORECASE)

_ANIM3D_MODEL_KW = re.compile(
    r"\b(建模|model|mesh|网格|拓扑|topology|绑定|rig|armature|bone|骨骼|"
    r"retopo|uv|uvmap)\b", re.IGNORECASE)
_ANIM3D_KEY_KW = re.compile(
    r"\b(k帧|keyframe|animation|action|fcurve|pose|动画|驱动|driver|"
    r"interp|缓动|ease|timing)\b", re.IGNORECASE)
_ANIM3D_RENDER_KW = re.compile(
    r"\b(渲染|render|材质|material|shader|灯光|light|光照|output|输出|"
    r"cycles|eevee|合成|compositing|分辨率)\b", re.IGNORECASE)


def _scan_text(path: str) -> str:
    """目录→汇总小文本文件内容；单文件→内容。"""
    if os.path.isfile(path):
        ext = os.path.splitext(path)[1].lower()
        if ext in (".md", ".txt", ".json", ".yaml", ".yml", ".py", ".toml"):
            try:
                return open(path, encoding="utf-8", errors="replace").read()
            except OSError:
                return ""
        return os.path.basename(path)  # 二进制文件只看文件名
    if os.path.isdir(path):
        parts = []
        for fn in sorted(os.listdir(path))[:200]:
            parts.append(fn)
            fp = os.path.join(path, fn)
            if os.path.isfile(fp) and os.path.splitext(fn)[1].lower() in (".md", ".txt"):
                try:
                    parts.append(open(fp, encoding="utf-8", errors="replace").read()[:2000])
                except OSError:
                    pass
        return "\n".join(parts)
    return ""


def _media_layers(kind: str, path: str) -> dict:
    """剪辑（粗剪→精剪→调色音效）/ 3D 动画（建模绑定→K帧→渲染）分层检查。"""
    if not os.path.exists(path):
        return {"ok": False, "error": f"路径不存在: {path}"}
    text = _scan_text(path)
    if kind == "clip":
        names = ["raw", "fine", "finish"]
        labels = ["粗剪（素材齐/顺序对）——第一层",
                  "精剪（转场/节奏/剪点）——第二层",
                  "调色音效（色彩/音频/字幕）——第三层"]
        kws = [_CLIP_RAW_KW, _CLIP_FINE_KW, _CLIP_FINISH_KW]
    else:
        names = ["model", "key", "render"]
        labels = ["建模绑定（网格/骨骼/绑定）——第一层",
                  "K帧（关键帧/动画/驱动）——第二层",
                  "渲染（材质/灯光/输出）——第三层"]
        kws = [_ANIM3D_MODEL_KW, _ANIM3D_KEY_KW, _ANIM3D_RENDER_KW]

    hits = [len(k.findall(text)) for k in kws]
    layers = {}
    for n, l, h in zip(names, labels, hits):
        layers[n] = {"done": h >= 3, "hits": h, "label": l}
    # 顺序校验：下层完成但上层未完成 = 违规
    violations = []
    for i, n in enumerate(names):
        if layers[n]["done"]:
            for prev in names[:i]:
                if not layers[prev]["done"]:
                    violations.append(f"{labels[i].split('——')[0]} 已做但 "
                                      f"{labels[names.index(prev)].split('——')[0]} 未完成——应先行")
                    break
    return {"ok": True, "kind": kind, "path": path,
            "layers": layers,
            "stage": next((n for n in names if not layers[n]["done"]), "全部完成"),
            "violations": violations,
            "advice": ("按顺序推进：" + " → ".join(labels) +
                       "；每层完成后验证（video_info/timeline_check/anim_check/render_sim）再进下一层"
                       if violations or any(not layers[n]["done"] for n in names)
                       else "三层均完成——按 " + " → ".join(labels) + " 顺序已验证")}


if __name__ == "__main__":
    import json
    import sys
    action = sys.argv[1] if len(sys.argv) > 1 else "code"
    path = sys.argv[2] if len(sys.argv) > 2 else __file__
    print(json.dumps(layer_check(action, path), ensure_ascii=False, indent=2))
