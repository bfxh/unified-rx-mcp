# -*- coding: utf-8 -*-
"""v31h 高压探针：点面=整面按格（用户：2×2×2 上"一个面只能有一个连接点是错误的，
按格子算"——反复强调 n 遍）

H1 2×2×2 点面顶面 → 4 格（用户截图场景）
H2 再点同面→整面取消；点其它面→该面铺满（多方向按格）
H3 1×1×1 点击=1 格（面=1 格时一致）
H4 gen_mp 主面每格（与点面一致）
H5 空心壳顶面 16 格（外部面全按格）
"""
import bpy, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importlib.util
spec = importlib.util.spec_from_file_location(
    "vf", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "voxelforge_connector.py"))
vf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vf)

PASS = FAIL = 0

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")

def face_on_cells(cells, lb, face):
    """该 face 全部暴露格（点面逻辑核心）"""
    occ = vf.occupied_set(cells)
    air = vf.external_air_cells(cells, lb)
    dx, dy, dz = vf.FACE_OFFSETS[face]
    out = []
    for (gx, gy, gz) in occ:
        if (gx + dx, gy + dy, gz + dz) in occ:
            continue
        if not vf.is_exposed_face((gx, gy, gz, face), cells, lb, air, occ):
            continue
        out.append((gx, gy, gz))
    return out

def click_face_sim(obj, face):
    """模拟点面：face 所有暴露格标记（toggle：已有则整面取消）"""
    cells = vf._occupied_cells(obj)
    lb = vf._local_bounds(obj)
    on = face_on_cells(cells, lb, face)
    old = list(obj.get("vf_connect_points", []))
    has_face = any(vf.mark_to_cell_face(m)[3] == face for m in old)
    keep = [m for m in old if vf.mark_to_cell_face(m)[3] != face]
    if has_face:
        obj["vf_connect_points"] = [list(m) for m in keep]
        return on, list(obj["vf_connect_points"]), "cancelled"
    marks = [vf.face_mark_from_cell_face(g, face) for g in on]
    obj["vf_connect_points"] = [list(m) for m in vf.merge_face_marks(keep + marks)]
    return on, list(obj["vf_connect_points"]), "added"

def mk_box(sx, sy, sz, loc=(0, 0, 0)):
    mesh = bpy.data.meshes.new("b")
    verts = []
    for x in (0.0, sx):
        for y in (0.0, sy):
            for z in (0.0, sz):
                verts.append((x, y, z))
    mesh.from_pydata(verts, [], [(0, 1, 3, 2), (4, 6, 7, 5), (0, 2, 6, 4),
                                 (1, 5, 7, 3), (0, 4, 5, 1), (2, 3, 7, 6)])
    mesh.update()
    obj = bpy.data.objects.new("b", mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj.location = loc
    bpy.context.view_layer.update()
    return obj

print("== H1: 2×2×2 点面顶面 → 4 格（用户截图场景）==")
bpy.ops.wm.read_factory_settings(use_empty=True)
o = mk_box(2, 2, 2)
on, marks, state = click_face_sim(o, "Top")
check("H1 顶面 4 格", len(on) == 4, f"got {on}")
check("H1 标记 4 个", len(marks) == 4, f"got {len(marks)}")

print("== H2: 再点同面→整面取消；点 East→该面铺满 ==")
on2, marks2, state2 = click_face_sim(o, "Top")
check("H2 再点顶面=整面取消", state2 == "cancelled" and
      len([m for m in marks2 if vf.mark_to_cell_face(m)[3] == "Top"]) == 0,
      f"state={state2} marks={len(marks2)}")
on3, marks3, state3 = click_face_sim(o, "East")
check("H2 点 East → 4 个 East 标记", state3 == "added" and
      len([m for m in marks3 if vf.mark_to_cell_face(m)[3] == "East"]) == 4,
      f"got {len(marks3)}")

print("== H3: 1×1×1 点击=1 格 ==")
bpy.ops.wm.read_factory_settings(use_empty=True)
o = mk_box(1, 1, 1)
on, marks, state = click_face_sim(o, "Top")
check("H3 单格模型 1 格 1 标记", len(on) == 1 and len(marks) == 1,
      f"on={on} marks={len(marks)}")

print("== H4: gen_mp 主面每格（与点面一致）==")
bpy.ops.wm.read_factory_settings(use_empty=True)
o = mk_box(2, 2, 2)
cells = vf._occupied_cells(o)
face, cells_on = vf.primary_face_for_module(cells, vf._local_bounds(o))
gen_marks = [vf.face_mark_from_cell_face(g, face) for g in cells_on]
check("H4 gen_mp 主面 4 格（与点面一致）", len(gen_marks) == 4,
      f"got {len(gen_marks)} {face}")

print("== H5: 空心壳顶面 16 格 ==")
bpy.ops.wm.read_factory_settings(use_empty=True)
o = mk_box(4, 4, 4)
cut = mk_box(2, 2, 2, (1, 1, 1))
m = o.modifiers.new("b", "BOOLEAN")
m.operation = "DIFFERENCE"
m.object = cut
bpy.context.view_layer.update()
cells = vf._occupied_cells(o)
lb = vf._local_bounds(o)
on = face_on_cells(cells, lb, "Top")
check("H5 空壳顶面 16 格（外表面全按格）", len(on) == 16, f"got {len(on)}")

print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
bpy.ops.wm.quit_blender()
sys.exit(1 if FAIL else 0)
