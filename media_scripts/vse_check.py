#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""vse_check.py — Blender VSE 时间线检查（media_core.timeline_check 调用）。

在 blender -b <blend> -P vse_check.py -- 下运行：
- 素材断链：MOVIE/IMAGE strip filepath 不存在
- 时长越界：strip frame_start+duration 超出 scene.frame_end
- 帧率/分辨率：scene.render 配置报告
输出：print("__MEDIA_JSON__" + json)
"""
import json
import os
import sys

try:
    import bpy  # noqa: F401
except ImportError:
    print("__MEDIA_JSON__" + json.dumps({"ok": False, "error": "非 Blender 环境"}))
    sys.exit(0)


def main():
    try:
        _run()
    except Exception as e:
        # 顶层兜底（审查 2026-08-17）：bpy 属性访问异常也必须输出 JSON——
        # 否则 media_core 报"脚本未输出结果"无法定位
        print("__MEDIA_JSON__" + json.dumps(
            {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"},
            ensure_ascii=False))


def _run():
    scene = bpy.context.scene
    se = getattr(scene, "sequence_editor", None)
    if se is None:
        print("__MEDIA_JSON__" + json.dumps(
            {"ok": True, "strips": 0, "broken": [], "out_of_range": [],
             "fps": scene.render.fps / scene.render.fps_base if scene.render.fps_base else 0,
             "resolution": [scene.render.resolution_x, scene.render.resolution_y],
             "issues": [], "advice": "无 VSE 序列（空时间线）"}))
        return

    strips = list(getattr(se, "sequences_all", None) or [])
    broken, out_of_range, issues = [], [], []
    media_types = {"MOVIE", "IMAGE", "SOUND", "AUDIO"}
    frame_end = scene.frame_end
    for st in strips:
        name = st.name
        # 断链
        fp = getattr(st, "filepath", "") or ""
        if fp and st.type in media_types:
            abs_fp = bpy.path.abspath(fp)
            if not os.path.isfile(abs_fp):
                broken.append({"name": name, "type": st.type, "path": abs_fp})
        # 时长越界
        start = st.frame_start
        dur = getattr(st, "frame_final_duration", st.frame_duration)
        if start + dur > frame_end + 1:
            out_of_range.append({"name": name, "start": start,
                                 "end": start + dur, "scene_end": frame_end})
        # 类型计数
    fps = scene.render.fps / scene.render.fps_base if scene.render.fps_base else 0
    res = [scene.render.resolution_x, scene.render.resolution_y]

    if broken:
        issues.append(f"{len(broken)} 个素材断链（文件缺失）")
    if out_of_range:
        issues.append(f"{len(out_of_range)} 个 strip 超出时间线末尾")
    # 帧率混用提示：strip 的 fps_source 不一致（MOVIE strip 有 fps_source）
    fps_sources = {}
    for st in strips:
        fs = getattr(st, "fps_source", None)
        if fs:
            fps_sources[fs] = fps_sources.get(fs, 0) + 1
    if len(fps_sources) > 1:
        issues.append(f"帧率来源混用: {fps_sources}")

    print("__MEDIA_JSON__" + json.dumps({
        "ok": not issues, "strips": len(strips),
        "broken": broken[:20], "out_of_range": out_of_range[:20],
        "fps": fps, "resolution": res,
        "fps_sources": {str(k): v for k, v in fps_sources.items()},
        "issues": issues[:20],
        "advice": ("时间线健康" if not issues else "；".join(issues[:8])),
    }, ensure_ascii=False))


main()
