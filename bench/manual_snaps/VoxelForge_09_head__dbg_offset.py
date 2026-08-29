# -*- coding: utf-8 -*-
"""debug：用户截图场景复现——模型未对齐（location 偏移 + scale 非 1）
时 gen_mp 与渲染是否错位。用户截图：绿面偏小偏移 + 橙框存在。
"""
import sys

sys.path.insert(0, r"D:\开发\VoxelForge\tools\blender")
import bpy
from mathutils import Vector
import voxelforge_connector as vf

print("=== 场景 1：模型偏移（location=(3.3,0.7,-2.4)）未对齐 ===")
for o in list(bpy.data.objects):
    bpy.data.objects.remove(o, do_unlink=True)
bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, 0))
obj = bpy.context.active_object
obj.name = "corp_off"
obj.scale = (1.3, 0.8, 1.1)  # 非单位缩放
obj.location = (3.3, 0.7, -2.4)
bpy.context.view_layer.objects.active = obj
bpy.ops.voxelforge.gen_mp()
marks = list(obj.get("vf_connect_points", []))
print("场景1 标记数:", len(marks))
for m in marks:
    print("  ", tuple(round(x, 3) for x in m))
print("  占格（_occupied_cells 需闭包——用 align 后验证）：")
bpy.ops.voxelforge.align_grid()
# 对齐后再看
print("  对齐后标记数:", len(marks))
print("  对齐后 location:", tuple(round(x, 3) for x in obj.location))
print("  对齐后 scale:", tuple(round(x, 3) for x in obj.scale))

print("=== 场景 2：对齐后但 gen_mp 与 _occupied_cells 占格一致性 ===")
# 占格检测（闭包不可直接调——用 gen_mp 标记反推）
cells_occ = vf.occupied_set([(0, 0, 0, True), (1, 0, 0, True)])
print("  手工 2×1 占格:", len(cells_occ))
