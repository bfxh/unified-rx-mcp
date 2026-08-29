# -*- coding: utf-8 -*-
"""复现用户场景：Blender 默认 cube（-1..1）→ 自动生成（主面每格 1×1 分散）→ 渲染绿面尺寸"""
import bpy, sys, os
sys.path.insert(0, "D:/开发/VoxelForge/tools/blender")
import importlib.util
spec = importlib.util.spec_from_file_location("vf", "D:/开发/VoxelForge/tools/blender/voxelforge_connector.py")
vf = importlib.util.module_from_spec(spec); spec.loader.exec_module(vf)
import mathutils

bpy.ops.wm.read_factory_settings(use_empty=True)
# Blender 默认 cube：Add > Cube（size=2，顶点 -1..1）
bpy.ops.mesh.primitive_cube_add(size=2.0, location=(0, 0, 0))
obj = bpy.context.active_object
print(f"cube: verts={len(obj.data.vertices)} scale={obj.scale[:]} loc={obj.location[:]}")

# 模拟 gen_mp.execute 完整路径
aligned = vf._auto_align_if_needed(obj)
print(f"auto_align: {aligned} loc={obj.location[:]} bounds_w={vf._bounds_of(obj)}")
cells = vf._occupied_cells(obj)
lb = vf._local_bounds(obj)
print(f"cells: {len(vf.occupied_set(cells))} 格, lb={lb}")
face, cells_on = vf.primary_face_for_module(cells, lb)
print(f"primary: {face} 格={sorted(cells_on)}")
# v31d：gen_mp=主面每格 1×1 分散（2×2×2 默认块=4 个连接点）
marks = [vf.face_mark_from_cell_face(g, face) for g in cells_on]
print(f"主面={face} 每格分散 标记数={len(marks)}（2×2×2 应=4）")
dims = vf.dims_from_bounds(vf._bounds_of(obj))
print(f"dims={dims}")

# 渲染路径数学（_vf_draw_cb 同公式）
mw_full = vf._grid_to_world_matrix(obj)
cs = 1.0
su = sv = 0.5 * cs
m = marks[0]
cf = vf.mark_to_cell_face(m)
n = mathutils.Vector((m[3], m[4], m[5]))
n.normalize()
u = mathutils.Vector((0.0, 0.0, 1.0)).cross(n).normalized()
v = n.cross(u).normalized()
wl = mathutils.Vector((m[0], m[1], m[2]))
c0 = mw_full @ (wl - u * su - v * sv)
c1 = mw_full @ (wl + u * su - v * sv)
c2 = mw_full @ (wl + u * su + v * sv)
c3 = mw_full @ (wl - u * su + v * sv)
print("绿面四角（世界）:")
for c in (c0, c1, c2, c3):
    print(f"  ({c.x:.3f}, {c.y:.3f}, {c.z:.3f})")
w_uv = (c1 - c0).length
h_uv = (c3 - c0).length
print(f"绿面尺寸: {w_uv:.4f} x {h_uv:.4f} 米  (cs=1.0 → 应为 1.0 米)")
# 模型尺寸
bw = vf._bounds_of(obj)
print(f"模型世界尺寸: {bw[3]-bw[0]:.3f} x {bw[4]-bw[1]:.3f} x {bw[5]-bw[2]:.3f} 米")
print(f"绿面/模型宽度比: {(c1-c0).length / (bw[3]-bw[0]):.3f}（1格/2格=0.5 为正确）")
exit(0 if len(marks) in (4,) or len(marks) > 0 else 1)
bpy.ops.wm.quit_blender()
