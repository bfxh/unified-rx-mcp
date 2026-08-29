# -*- coding: utf-8 -*-
"""v29：复现用户截图——绿面出现在模型内部（半透明大立方体内部两个绿面）。

场景矩阵：
A) 均匀放大 scale=(2,2,2) gen_mp（应 Top 面 y=2）
B) 非均匀 scale=(3,1,2) gen_mp
C) 点击侧面（East/West）→ 标记世界位置 vs 模型 bbox
D) 空心壳（2×2×2 外壳）→ 内壁 is_exposed 判定（内壁该不该标？）
E) 批量展开后标记集合（应全部在主面格）
"""
import sys
sys.path.insert(0, r"D:\开发\VoxelForge\tools\blender")
import bpy
from mathutils import Vector, Matrix

ok = 0
fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"[OK] {name} {detail}")
    else:
        fail += 1
        print(f"[FAIL] {name} {detail}")


def world_bbox(obj):
    bb = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    return (tuple(round(min(b[i] for b in bb), 3) for i in range(3)),
            tuple(round(max(b[i] for b in bb), 3) for i in range(3)))


def reset(scale, loc=(0, 0, 0)):
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, 0))
    obj = bpy.context.active_object
    obj.scale = scale
    obj.location = loc
    bpy.context.view_layer.objects.active = obj
    bpy.ops.voxelforge.align_grid()
    return obj


import voxelforge_connector as vf

# ── A) 均匀放大 ──
obj = reset((2.0, 2.0, 2.0))
bpy.ops.voxelforge.gen_mp()
marks = list(obj.get("vf_connect_points", []))
mn, mx = world_bbox(obj)
print(f"[A] bbox={mn}->{mx} loc={tuple(obj.location)} scale={tuple(obj.scale)}")
for m in marks:
    w = Matrix.Translation(obj.location) @ Vector(m[0:3])
    print(f"[A] 标记世界={tuple(round(x,3) for x in w)} 法向={m[3:6]}")
ins = all(mn[0] <= w.x <= mx[0] and mn[1] <= w.y <= mx[1] and mn[2] <= w.z <= mx[2]
          for m in marks for w in [Matrix.Translation(obj.location) @ Vector(m[0:3])])
check("A: 标记都在 bbox 内", ins)
on_surf = any(abs(w.y - mx[1]) < 1e-3 for m in marks
              for w in [Matrix.Translation(obj.location) @ Vector(m[0:3])])
check("A: 主面=Top 在表面", on_surf)

# ── B) 非均匀 ──
obj = reset((3.0, 1.0, 2.0))
bpy.ops.voxelforge.gen_mp()
mn, mx = world_bbox(obj)
marks = list(obj.get("vf_connect_points", []))
print(f"[B] bbox={mn}->{mx} 标记数={len(marks)}")
for m in marks:
    w = Matrix.Translation(obj.location) @ Vector(m[0:3])
    print(f"[B] 标记世界={tuple(round(x,3) for x in w)} 法向={m[3:6]}")
# 每个标记的格面应贴合 bbox 表面（x=mn/mx 或 y=mn/mx 或 z=mn/mx）
surf_hit = 0
for m in marks:
    w = Matrix.Translation(obj.location) @ Vector(m[0:3])
    if any(abs(w[i] - mn[i]) < 1e-3 or abs(w[i] - mx[i]) < 1e-3 for i in range(3)):
        surf_hit += 1
check(f"B: 标记贴合表面 {surf_hit}/{len(marks)}", surf_hit == len(marks))

# ── C) 点击侧面（East）——align 后 bbox 真实值验证 ──
obj = reset((3.0, 1.0, 1.0))
mn2, mx2 = world_bbox(obj)
cells_real = [(0, 0, 0, True), (1, 0, 0, True), (2, 0, 0, True)]
m = vf.face_mark_from_cell_face((2, 0, 0), "East")
w = Matrix.Translation(obj.location) @ Vector(m[0:3])
print(f"[C] bbox x={mn2[0]}->{mx2[0]} East(2,0,0) 世界 x={w[0]:.3f}")
check("C: East 标记在 bbox 表面上", abs(w[0] - mx2[0]) < 1e-3,
      f"（w={tuple(round(x,3) for x in w)} bbox 右缘={mx2[0]}）")
check("C: East 暴露", vf.is_exposed_face((2, 0, 0, "East"), cells_real))
# 埋藏面（相邻有占用格）→ 不暴露：2×2×2 内部格 (1,1,1) East 相邻 (2,1,1)
check("C: 埋藏面不暴露",
      not vf.is_exposed_face(
          (1, 1, 1, "East"),
          [(1, 1, 1, True), (2, 1, 1, True)]))

# ── D) 4×4×4 大壳（盒壁 1 格）→ 内壁是否被判暴露？──
# 外 4×4×4，内 2×2×2 空腔；bounds=(0,0,0,4,4,4)
shell_cells = []
for x in range(4):
    for y in range(4):
        for z in range(4):
            inner = 1 <= x <= 2 and 1 <= y <= 2 and 1 <= z <= 2
            shell_cells.append((x, y, z, not inner))
bounds4 = (0.0, 0.0, 0.0, 4.0, 4.0, 4.0)
# 空腔壁（(1,1,1) Top 指向空腔 (1,2,1)）——应不暴露（带 bounds）
cavity = (1, 1, 1, "Top")
print(f"[D] 空腔壁 无bounds={vf.is_exposed_face(cavity, shell_cells)} "
      f"带bounds={vf.is_exposed_face(cavity, shell_cells, bounds4)}")
check("D: 空腔壁面带 bounds 不暴露",
      not vf.is_exposed_face(cavity, shell_cells, bounds4))
# 外壳格朝向空腔的内壁面：(0,1,1) East 相邻 (1,1,1)=空腔格
inner_wall = (0, 1, 1, "East")
check("D: 外壳内壁面不暴露",
      not vf.is_exposed_face(inner_wall, shell_cells, bounds4))
# 外壳内壁 Top（(0,1,1) 相邻 (0,2,1)? inner False（x=0 非 inner）→ 占用 → 埋藏
check("D: 内壁 East 无 bounds 误判暴露（原 bug）",
      vf.is_exposed_face(inner_wall, shell_cells))
# 外部 Top（(3,3,3) Top 相邻 (3,4,3) 空）应为暴露（带 bounds）
outer = (3, 3, 3, "Top")
check("D: 外壳外表面带 bounds 暴露",
      vf.is_exposed_face(outer, shell_cells, bounds4))
# 底面 (3,0,3) Bottom 朝外——暴露
check("D: 外底面带 bounds 暴露",
      vf.is_exposed_face((3, 0, 3, "Bottom"), shell_cells, bounds4))
# 主面选择：空腔壁不入选——外壳外侧（Top 16 格）应为主面
f4, cells4 = vf.primary_face_for_module(shell_cells, bounds4)
check("D: 主面=外表面（非空腔壁）", f4 == "Top" and len(cells4) == 16,
      f"（face={f4}, cells={len(cells4)}——期望 Top 16）")

# ── E) 批量展开终态：标记应全部在主面 ──
rings_all = vf.face_expand_rings("Top", [(0, 2, 0), (0, 2, 1), (1, 2, 0), (1, 2, 1)], (0, 2, 0))
check("E: 批量环覆盖主面全格", sum(len(r) for r in rings_all) == 4)

print(f"v29 结果: {ok} 通过 / {fail} 失败")
sys.exit(1 if fail else 0)
