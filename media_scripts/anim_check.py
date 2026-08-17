#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""anim_check.py — Blender 场景动画检查（media_core.anim_check 调用）。

在 blender -b <blend> -P anim_check.py -- 下运行：
- action/关键帧：对象 animation_data.action 的 fcurves/keyframe_points
- 骨骼：armature 对象 bones/pose_bones
- 蒙皮：mesh 的 ARMATURE modifier
- 驱动器：animation_data.drivers
输出：print("__MEDIA_JSON__" + json)
"""
import json
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
        # 顶层兜底（审查 2026-08-17）：bpy 属性访问异常也必须输出 JSON
        print("__MEDIA_JSON__" + json.dumps(
            {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"},
            ensure_ascii=False))


def _run():
    scene = bpy.context.scene
    issues = []
    animated = []
    total_keyframes = 0
    armatures = 0
    bones = 0
    skinned_meshes = 0
    drivers = 0

    for obj in scene.objects:
        ad = obj.animation_data
        if ad is not None:
            if ad.action is not None:
                n_fc = len(ad.action.fcurves)
                n_kf = sum(len(fc.keyframe_points) for fc in ad.action.fcurves)
                total_keyframes += n_kf
                if n_fc == 0:
                    issues.append(f"对象 '{obj.name}' 的 action '{ad.action.name}' 无 fcurve（空动画）")
                animated.append({"object": obj.name, "action": ad.action.name,
                                 "fcurves": n_fc, "keyframes": n_kf})
            if ad.drivers:
                drivers += len(ad.drivers)
        if obj.type == "ARMATURE":
            armatures += 1
            bones += len(obj.data.bones) if obj.data else 0
        if obj.type == "MESH":
            for mod in obj.modifiers:
                if mod.type == "ARMATURE":
                    skinned_meshes += 1
                    break

    if armatures == 0 and any(o.type == "MESH" for o in scene.objects):
        issues.append("场景有网格但无骨架（建模未绑定）")
    if skinned_meshes == 0 and armatures > 0:
        issues.append("有骨架但无网格蒙皮（绑定未完成）")
    if total_keyframes == 0 and animated:
        issues.append("有动画对象但关键帧总数为 0")

    print("__MEDIA_JSON__" + json.dumps({
        "ok": not issues, "animated_objects": animated[:20],
        "action_count": len(animated),
        "total_keyframes": total_keyframes,
        "armatures": armatures, "bones": bones,
        "skinned_meshes": skinned_meshes, "drivers": drivers,
        "frame_start": scene.frame_start, "frame_end": scene.frame_end,
        "issues": issues[:20],
        "advice": ("动画数据完整" if not issues else "；".join(issues[:8])),
    }, ensure_ascii=False))


main()
