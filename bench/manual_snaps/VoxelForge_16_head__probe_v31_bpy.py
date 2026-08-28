# -*- coding: utf-8 -*-
"""v31 bpy 集成探针（Blender headless）：验证连接点链路在真实对象上的行为。

场景：1×1×1 cube（顶点 0..1，建模空间）scale=(2,2,2) location=(5,0,0)
- _local_bounds = (0,0,0,2,2,2)（格空间）
- 顶面世界 y=2 + loc.y=0 → 格 (0,1,0) Top
检查：
A1 _occupied_cells 格范围 = 0..2（2³ 占格 8）
A2 _mark_from_local 顶面 → (0.5, 2, 0.5) 格空间（旧路径 y=1 错误）
A3 渲染世界位置 = loc + 格空间 ✓
A4 is_exposed_face（air 连通集）顶面暴露/底面暴露 + 无空腔
A5 主面 primary_face_for_module = Top 4 格（2×2）
A6 批量展开环序 face_expand_rings 从中心格
A7 face_mark_to_mount_point offset（面内移动后）
"""
import bpy
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importlib.util
spec = importlib.util.spec_from_file_location(
    "vf", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "voxelforge_connector.py"))
vf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vf)
assert vf.HAS_BPY, "bpy 未加载"
import mathutils

PASS = FAIL = 0

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")

# 清空场景
bpy.ops.wm.read_factory_settings(use_empty=True)

# 造 0..1 建模空间 cube（顶点 8、每轴尺寸 1）
mesh = bpy.data.meshes.new("test_cube")
verts = [(x, y, z) for x in (0.0, 1.0) for y in (0.0, 1.0) for z in (0.0, 1.0)]
faces = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 2, 6, 4),
         (1, 5, 7, 3), (0, 4, 5, 1), (2, 3, 7, 6)]
mesh.from_pydata(verts, [], faces)
mesh.update()
obj = bpy.data.objects.new("test_cube", mesh)
bpy.context.scene.collection.objects.link(obj)
obj.scale = (2.0, 2.0, 2.0)
obj.location = (5.0, 0.0, 0.0)
bpy.context.view_layer.update()

print("== A1: 占格 ==")
cells = vf._occupied_cells(obj)
occ = vf.occupied_set(cells)
print(f"  占格 {sorted(occ)}")
check("A1 8 格全部占用（2³）", len(occ) == 8, f"got {len(occ)}")
check("A1 格范围 0..2（世界米，非建模空间 0..1）",
      min(c[0] for c in occ) == 0 and max(c[0] for c in occ) == 1
      and min(c[1] for c in occ) == 0 and max(c[1] for c in occ) == 1
      and min(c[2] for c in occ) == 0 and max(c[2] for c in occ) == 1)

print("== A2: _mark_from_local 顶面（scale=2）==")
# 点击顶面：世界中心 (5.5, 2, 0.5) → 局部 (0.25, 1, 0.25)
inv = obj.matrix_world.inverted()
center_local = inv @ mathutils.Vector((5.5, 2.0, 0.5))
normal_world = mathutils.Vector((0.0, 1.0, 0.0))
normal_local = inv.to_3x3() @ normal_world
mark = vf._mark_from_local(
    obj, (center_local.x, center_local.y, center_local.z),
    (normal_local.x, normal_local.y, normal_local.z))
print(f"  mark = {mark}")
check("A2 顶面标记 = 格空间 (0.5, 2, 0.5)（y=2 顶面）",
      abs(mark[0] - 0.5) < 1e-6 and abs(mark[1] - 2.0) < 1e-6
      and abs(mark[2] - 0.5) < 1e-6, f"got {mark}")
cf = vf.mark_to_cell_face(mark)
check("A2 反推 cell = (0,1,0) Top", cf == (0, 1, 0, "Top"), f"got {cf}")

print("== A3: 渲染世界位置 ==")
mw = vf._grid_to_world_matrix(obj)
wl = mw @ mathutils.Vector((mark[0], mark[1], mark[2]))
print(f"  渲染中心世界 = {wl[:]}")
check("A3 渲染中心 = (5.5, 2, 0.5)（顶面中心）",
      abs(wl.x - 5.5) < 1e-6 and abs(wl.y - 2.0) < 1e-6
      and abs(wl.z - 0.5) < 1e-6, f"got {wl[:]}")

print("== A4: 暴露判定（air 连通集）==")
lb = vf._local_bounds(obj)
air = vf._air_for(obj, cells, lb)
print(f"  bounds={lb} air={sorted(air)}")
check("A4 顶面格 Top 暴露", vf.is_exposed_face((0, 1, 0, "Top"), cells, lb, air))
check("A4 底面格 Bottom 暴露", vf.is_exposed_face((0, 0, 0, "Bottom"), cells, lb, air))
check("A4 埋藏面（(0,0,0) Top 邻格占用）不暴露",
      not vf.is_exposed_face((0, 0, 0, "Top"), cells, lb, air))

print("== A5: 主面 ==")
face, cells_on = vf.primary_face_for_module(cells, lb)
print(f"  主面 {face} 格 {sorted(cells_on)}")
check("A5 主面 Top 4 格（2×2 顶面整面）",
      face == "Top" and len(cells_on) == 4, f"got {face} {cells_on}")

print("== A6: 批量展开环序 ==")
center = vf.face_center_cell(face, cells_on)
rings = vf.face_expand_rings(face, cells_on, center)
print(f"  中心 {center} 环序 {[[g for g in r] for r in rings]}")
total = sum(len(r) for r in rings)
check("A6 环序第一环=中心格", rings[0] == [center], f"got {rings[0]}")
check("A6 环覆盖全部 4 格", total == 4, f"got {total}")

print("== A7: 导出 mount_point（含面内移动 offset）==")
mp0 = vf.face_mark_to_mount_point(mark, (2, 2, 2))
print(f"  中心标记 mp = {mp0}")
check("A7 cell/face 正确", mp0[:4] == (0, 1, 0, "Top"), f"got {mp0}")
check("A7 中心标记 offset=(0,0,0)", mp0[7] == (0.0, 0.0, 0.0), f"got {mp0[7]}")
# 面内移动：Top 面内移 x +0.3
moved = (0.8, 2.0, 0.5, 0.0, 1.0, 0.0)
mp1 = vf.face_mark_to_mount_point(moved, (2, 2, 2))
print(f"  面内移动标记 mp = {mp1}")
check("A7 移动后 cell 不变 (0,1,0)", mp1 is not None and mp1[:4] == (0, 1, 0, "Top"),
      f"got {mp1}")
check("A7 offset 携带 x 偏移 +0.3",
      mp1 is not None and abs(mp1[7][0] - 0.3) < 1e-6, f"got {mp1[7]}")

print("== A8: 空壳（挖腔）回归 ==")
mesh2 = bpy.data.meshes.new("shell")
# 4³ 外壳挖 2³：构建空心壳顶点（复杂）——用 3³ 挖 1³ 的格数据直接判
box3 = [(x, y, z, True) for x in range(3) for y in range(3) for z in range(3)
        if not (x == 1 and y == 1 and z == 1)]
lb3 = (0, 0, 0, 3, 3, 3)
air3 = vf.external_air_cells(box3, lb3)
print(f"  3³挖1³: air={sorted(air3)} 腔格(1,1,1)应不在其中")
check("A8 腔格 (1,1,1) 不通外部（空腔）", (1, 1, 1) not in air3)
check("A8 腔壁面判内壁", not vf.is_exposed_face((1, 0, 1, "East"), box3, lb3, air3))
check("A8 外表面判暴露", vf.is_exposed_face((0, 0, 0, "West"), box3, lb3, air3))

print("== A9: 缓存指纹（顶点平移后 key 变）==")
key_before = vf._grid_cache_key(obj)
for v in obj.data.vertices:
    v.co.x += 0.0  # 不动
key_same = vf._grid_cache_key(obj)
check("A9 未改顶点 key 相同", key_before == key_same)
for v in obj.data.vertices:
    v.co.x += 0.5
key_moved = vf._grid_cache_key(obj)
check("A9 顶点平移后 key 变化（缓存失效）", key_before != key_moved)

print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
bpy.ops.wm.quit_blender()
sys.exit(1 if FAIL else 0)
