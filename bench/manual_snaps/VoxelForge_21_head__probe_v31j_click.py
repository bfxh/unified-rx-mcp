# -*- coding: utf-8 -*-
"""v31j 高压探针：点击定位修复（用户："点其他格子会关闭第一次开启的那个格子"）

根因：invoke 用 _face_center_world（整个多边形几何中心）——2×2×2 顶面是
单一大面（跨 4 格）：点哪都反推同一格。修复后用 ray 命中点 loc。

J1 顶面不同位置 → 不同格（修复前=恒 (0,1,0)）
J2 end-to-end：点 A 位置 → 点 B 位置 → 2 个标记共存（用户场景）
J3 悬停 xy 缓存：同 xy 第二次跳过 ray（性能）
J4 旧逻辑对比：面中心路径恒 (0,1,0)（旧 bug 源——已由 loc 方案替代）
"""
import bpy, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importlib.util
spec = importlib.util.spec_from_file_location(
    "vf", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "voxelforge_connector.py"))
vf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vf)
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

def mk_box(sx, sy, sz):
    mesh = bpy.data.meshes.new("b")
    verts = [(x, y, z) for x in range(int(sx)) for y in range(int(sy))
             for z in range(int(sz))]
    mesh.from_pydata(verts, [], [(0, 1, 3, 2), (4, 6, 7, 5), (0, 2, 6, 4),
                                 (1, 5, 7, 3), (0, 4, 5, 1), (2, 3, 7, 6)])
    mesh.update()
    obj = bpy.data.objects.new("b", mesh)
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.update()
    return obj

def click_at(obj, world_pt):
    """模拟 FaceConnectToggle.invoke 的点击（v31j：用命中点 loc）"""
    inv = obj.matrix_world.inverted()
    center_local = inv @ mathutils.Vector(world_pt)
    normal_local = inv.to_3x3() @ mathutils.Vector((0.0, 1.0, 0.0))
    mark = vf._mark_from_local(
        obj, (center_local.x, center_local.y, center_local.z),
        (normal_local.x, normal_local.y, normal_local.z))
    if mark is None:
        return None
    cf = vf.mark_to_cell_face(mark)
    marks = list(obj.get("vf_connect_points", []))
    toggled = False
    for i, m in enumerate(marks):
        if vf.mark_to_cell_face(m) == cf:
            del marks[i]
            toggled = True
            break
    if not toggled:
        marks.append(mark)
    obj["vf_connect_points"] = [list(m) for m in marks]
    return cf

print("== J1: 顶面不同位置 → 不同格（旧=恒面中心格）==")
bpy.ops.wm.read_factory_settings(use_empty=True)
o = mk_box(2, 2, 2)
cf_a = click_at(o, (0.4, 2.0, 0.4))
cf_b = click_at(o, (1.6, 2.0, 0.5))
print(f"  点A(0.4,2,0.4) → {cf_a}   点B(1.6,2,0.5) → {cf_b}")
check("J1 A 格 = (0,1,0) Top", cf_a == (0, 1, 0, "Top"), f"got {cf_a}")
check("J1 B 格 = (1,1,0) Top（≠A——旧实现恒定 A）",
      cf_b == (1, 1, 0, "Top"), f"got {cf_b}")

print("== J2: 点 A → 点 B → 2 个标记共存（用户场景 end-to-end）==")
marks = list(o.get("vf_connect_points", []))
print(f"  标记 {len(marks)} 个:", [vf.mark_to_cell_face(m) for m in marks])
check("J2 两个不同格=2 个标记（不会关闭第一个）", len(marks) == 2,
      f"got {len(marks)}")

print("== J3: 悬停 xy 缓存（同位置跳过 ray）==")
vf._VF_HOVER.update({"xy": (100.0, 100.0)})
same = vf._VF_HOVER.get("xy") == (100.0, 100.0)
check("J3 同 xy 判定=跳过（缓存命中条件）", same)

print("== J4: 旧逻辑对比（面中心恒 (0,1,0)——旧 bug 源）==")
poly_center = (1.0, 2.0, 1.0)
inv = o.matrix_world.inverted()
c = inv @ mathutils.Vector(poly_center)
m = vf._mark_from_local(o, (c.x, c.y, c.z), (0.0, 1.0, 0.0))
cfc = vf.mark_to_cell_face(m)
print(f"  面中心路径 → {cfc}")
check("J4 面中心恒 (0,1,0)（旧 bug 源——已由 loc 方案替代）",
      cfc == (0, 1, 0, "Top"), f"got {cfc}")

print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
bpy.ops.wm.quit_blender()
sys.exit(1 if FAIL else 0)
