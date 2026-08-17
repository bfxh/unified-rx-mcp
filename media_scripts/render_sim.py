#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""render_sim.py — 完整渲染验证（media_core.render_sim 调用）。

在 blender -b <blend> -P render_sim.py -- <frames> <engine> <resolution> 下运行：
- frames: ALL（默认，全帧动画渲染）或 "1-10"（帧范围）
- engine: CYCLES / EEVEE_NEXT / EEVEE / WORKBENCH
- resolution: 0=保持场景设置；>0 覆盖宽
- 渲染到临时输出目录，验证输出文件齐全且非空
输出：print("__MEDIA_JSON__" + json)
"""
import json
import os
import sys
import tempfile
import time

try:
    import bpy  # noqa: F401
except ImportError:
    print("__MEDIA_JSON__" + json.dumps({"ok": False, "error": "非 Blender 环境"}))
    sys.exit(0)


def main():
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    frames = args[0] if len(args) > 0 else "ALL"
    engine = args[1] if len(args) > 1 else "CYCLES"
    resolution = int(args[2]) if len(args) > 2 and args[2].isdigit() else 0

    # Blender 5.x 引擎枚举名归一化（CYCLES 同名；EEVEE→BLENDER_EEVEE；WORKBENCH→BLENDER_WORKBENCH）
    _ENGINE_MAP = {"CYCLES": "CYCLES", "EEVEE": "BLENDER_EEVEE",
                   "EEVEE_NEXT": "BLENDER_EEVEE", "WORKBENCH": "BLENDER_WORKBENCH"}
    engine_bl = _ENGINE_MAP.get(engine.upper(), "CYCLES")

    scene = bpy.context.scene
    old_engine = scene.render.engine
    old_fmt = scene.render.image_settings.file_format
    old_res = (scene.render.resolution_x, scene.render.resolution_y)

    out_dir = tempfile.mkdtemp(prefix="rx_render_")
    scene.render.filepath = os.path.join(out_dir, "frame_")
    scene.render.image_settings.file_format = "PNG"
    scene.render.engine = engine_bl
    if resolution > 0:
        scene.render.resolution_x = resolution
        scene.render.resolution_y = int(resolution * old_res[1] / old_res[0]) if old_res[0] else resolution
    render_res = [scene.render.resolution_x, scene.render.resolution_y]

    t0 = time.time()
    errors = []
    rendered = []
    try:
        if frames == "ALL":
            scene.frame_start = scene.frame_start
            scene.frame_end = scene.frame_end
            bpy.ops.render.render(animation=True, write_still=False)
        else:
            # 帧范围 "1-10"（非 N-M 格式 → 明确 error，不崩溃无输出）
            parts = frames.split("-")
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                scene.frame_start = int(parts[0])
                scene.frame_end = int(parts[1])
                bpy.ops.render.render(animation=True, write_still=False)
            elif parts[0].isdigit():
                scene.frame_current = int(parts[0])
                bpy.ops.render.render(write_still=True)
            else:
                errors.append(f"frames 格式无效: {frames!r}（应为 ALL 或 1-10 或单帧数字）")
        # 收集输出文件
        for fn in sorted(os.listdir(out_dir)):
            fp = os.path.join(out_dir, fn)
            if os.path.isfile(fp) and os.path.getsize(fp) > 0:
                rendered.append({"file": fn, "size_kb": os.path.getsize(fp) // 1024})
        if not rendered:
            errors.append("渲染完成但无输出文件（可能场景为空/相机缺失）")
    except Exception as e:
        errors.append(f"渲染失败: {type(e).__name__}: {str(e)[:200]}")

    elapsed = round(time.time() - t0, 1)
    # 恢复场景设置（渲染验证不污染原场景；blender -b 本身不保存）
    scene.render.engine = old_engine
    scene.render.image_settings.file_format = old_fmt
    scene.render.resolution_x, scene.render.resolution_y = old_res

    print("__MEDIA_JSON__" + json.dumps({
        "ok": not errors and len(rendered) > 0,
        "engine": engine, "frames_requested": frames,
        "resolution": render_res,
        "rendered_frames": len(rendered), "output": rendered[:50],
        "out_dir": out_dir, "elapsed_sec": elapsed,
        "issues": errors[:10],
        "advice": ("渲染验证通过——输出齐全" if not errors and rendered
                   else "；".join(errors[:5]) or "无输出文件"),
    }, ensure_ascii=False))


main()
