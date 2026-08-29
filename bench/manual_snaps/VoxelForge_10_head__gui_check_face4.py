# -*- coding: utf-8 -*-
"""GUI 验证：1×1 模块顶面绿面（--factory-startup 无欢迎 splash）。

用户截图：1×1 立方体顶面绿面偏小/偏移（应覆盖整格面中央）。
复现：立方体 → align_grid → gen_mp → 截图 → 像素分析绿面 bbox。
"""
import bpy
import math
import sys

OUT = r"D:\开发\VoxelForge\screenshots\blender_face_check4.png"
sys.path.insert(0, r"D:\开发\VoxelForge\tools\blender")


def setup():
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, 0))
    obj = bpy.context.active_object
    obj.name = "corp_1x1"
    obj.scale = (1.0, 1.0, 1.0)
    obj.location = (0.5, 0.5, 0.5)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.voxelforge.align_grid()
    bpy.ops.voxelforge.gen_mp()
    marks = list(obj.get("vf_connect_points", []))
    print(f"[face4] 标记数: {len(marks)}（期望 1 = 顶面）")
    for m in marks:
        print(f"[face4]   mark {tuple(round(x, 2) for x in m)}")
    cam = bpy.data.cameras.new("Cam")
    cam_obj = bpy.data.objects.new("Cam", cam)
    bpy.context.scene.collection.objects.link(cam_obj)
    cam_obj.location = (1.2, 1.5, 2.0)
    cam_obj.rotation_euler = (math.radians(58), 0, math.radians(36))
    bpy.context.scene.camera = cam_obj
    for area in bpy.context.screen.areas:
        if area.type == "VIEW_3D":
            area.spaces.active.region_3d.view_perspective = "PERSP"
            area.spaces.active.region_3d.view_location = (0.5, 0.5, 0.5)
            area.spaces.active.region_3d.view_distance = 3.5


def snap(_t=None):
    try:
        # debug（2026-08-22 绿面消失排查）：读取 draw 计数与模块状态
        import voxelforge_connector as _vf
        print(f"[face4] draw_count={_vf._VF_DRAW_COUNT.get('n', '?')}")
        bpy.ops.screen.screenshot(filepath=OUT)
        print(f"[face4] screenshot saved")
        # 手工复刻渲染段：验证 tri_pts 能生成（headless 逻辑）
        import voxelforge_connector as vf
        marks = [tuple(m) for m in bpy.data.objects["corp_1x1"].get("vf_connect_points", [])]
        print(f"[face4] marks={len(marks)}")
        import mathutils
        obj = bpy.data.objects["corp_1x1"]
        lb = (0.0, 0.0, 0.0, 1.0, 1.0, 1.0)
        for (face, (u0, v0, u1, v1), n_axis, anchor_cell) in vf.merge_adjacent_face_marks(marks):
            n = mathutils.Vector(n_axis).normalized()
            _res = vf.face_mark_world_center((0, 0, 0, n.x, n.y, n.z), lb)
            wl = mathutils.Vector((_res[0], _res[1], _res[2]))
            su, sv = _res[4], _res[5]
            print(f"[face4] face={face} wl={tuple(round(x,2) for x in wl)} su={su} sv={sv}")
    except Exception as e:
        print(f"[face4] debug failed: {e}")
    bpy.ops.wm.quit_blender()


setup()
bpy.app.timers.register(snap, first_interval=15.0)
print("[face4] timer registered")
