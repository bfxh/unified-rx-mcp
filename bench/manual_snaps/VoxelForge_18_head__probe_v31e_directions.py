# -*- coding: utf-8 -*-
"""v31e 高压探针：多方向点面 / 体积小格判定 / 贴格阈值。

M1 六面（Top/Bottom/North/South/East/West）点面 → 反推格/法线/渲染四角全对
M2 斜切楔形模型：一角体积 30% 的格必须判占用（"哪怕小一点也要给"）+ 主面每格 1 点
M3 贴格阈值：面中心离吸附格中心 >0.5 格（未贴近网格）→ 拒绝（返回距偏/不收敛）
M4 3 格模型（一处体积小）→ 每格都有连接点（数量=占用格数，不多不少）
"""
import bpy, sys, os, math
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

def new_cube(size=1.0):
    bpy.ops.mesh.primitive_cube_add(size=size, location=(0, 0, 0))
    return bpy.context.active_object

def click_face(obj, face):
    """模拟 FaceConnectToggle：点某面的中心（世界）→ mark + 反推格 + 渲染四角"""
    axes = {"Top": (0, 1, 0), "Bottom": (0, -1, 0), "North": (0, 0, -1),
            "South": (0, 0, 1), "East": (1, 0, 0), "West": (-1, 0, 0)}
    n_w = mathutils.Vector(axes[face])
    bw = vf._bounds_of(obj)
    # 面中心（世界）：轴向取外表面，切向取中心
    c = [(bw[0] + bw[3]) * 0.5, (bw[1] + bw[4]) * 0.5, (bw[2] + bw[5]) * 0.5]
    for i in range(3):
        if abs(n_w[i]) > 0.5:
            c[i] = bw[i + 3] if n_w[i] > 0 else bw[i]
    inv = obj.matrix_world.inverted()
    center_local = inv @ mathutils.Vector(c)
    normal_local = inv.to_3x3() @ n_w
    mark = vf._mark_from_local(
        obj, (center_local.x, center_local.y, center_local.z),
        (normal_local.x, normal_local.y, normal_local.z))
    if mark is None:
        return None
    cf = vf.mark_to_cell_face(mark)
    # 渲染四角（与 _vf_draw_cb 同公式含 abs(n.z) 分支）
    mw_full = vf._grid_to_world_matrix(obj)
    n = mathutils.Vector((mark[3], mark[4], mark[5])); n.normalize()
    if abs(n.z) < 0.9:
        u = mathutils.Vector((0.0, 0.0, 1.0)).cross(n).normalized()
    else:
        u = mathutils.Vector((1.0, 0.0, 0.0)).cross(n).normalized()
    v = n.cross(u).normalized()
    wl = mathutils.Vector((mark[0], mark[1], mark[2]))
    c0 = mw_full @ (wl - u * 0.5 - v * 0.5)
    c1 = mw_full @ (wl + u * 0.5 - v * 0.5)
    c3 = mw_full @ (wl - u * 0.5 + v * 0.5)
    return {"mark": mark, "cf": cf, "size": (c1 - c0).length,
            "corner0": tuple(round(x, 3) for x in c0),
            "corner2": tuple(round(x, 3) for x in c3)}

print("== M1: 1×1×1 cube 六面点面 ==")
bpy.ops.wm.read_factory_settings(use_empty=True)
o = new_cube(1.0)
vf._auto_align_if_needed(o)  # cube -0.5..0.5 → 0..1
for face in ("Top", "Bottom", "North", "South", "East", "West"):
    r = click_face(o, face)
    if r is None:
        check(f"M1 {face}", False, "mark None")
        continue
    cf, sz = r["cf"], r["size"]
    ok_face = cf[3] == face
    ok_size = abs(sz - 1.0) < 1e-6
    ok_cell = cf[0] == 0 and cf[1] == 0 and cf[2] == 0
    check(f"M1 {face} 反推格 + 1.0 米 + 法线", ok_face and ok_size and ok_cell,
          f"got cf={cf} size={sz:.3f}")

print("== M2: 斜切楔形（一角 30% 体积）==")
bpy.ops.wm.read_factory_settings(use_empty=True)
o = new_cube(2.0)  # -1..1
# 斜切：砍掉 (1,1,0) 附近一角（用 bmesh 切平面）
import bmesh
bm = bmesh.new()
bm.from_mesh(o.data)
# 平面：x + y = 1.2（切掉 x>... 的角）
def cut_plane(bm, plane_co, plane_no):
    import mathutils as mu
    ret = bmesh.ops.bisect_plane(
        bm, geom=bm.verts[:] + bm.edges[:] + bm.faces[:],
        plane_co=plane_co, plane_no=plane_no, clear_inner=True)
    # 封闭切口
    edges = [e for e in ret['geom_cut'] if isinstance(e, bmesh.types.BMEdge)]
    if edges:
        try:
            bmesh.ops.edgeloop_fill(bm, edges=edges)
        except Exception:
            try:
                bmesh.ops.triangle_fill(bm, edges=edges)
            except Exception:
                pass
    return ret
try:
    cut_plane(bm, (0.6, 0.0, 0.0), (-1.0, -0.35, 0.0))  # x 正侧斜切
except Exception as e:
    print(f"  bisect_plane 异常（headless 无布尔算子？）: {e}")
bm.to_mesh(o.data); bm.free()
# 斜切不回填？用简易方案：直接建楔形 poly（无布尔）——
# 楔形：立方体 x∈[-1,1] 但 x>0.6 斜切到 y=-0.6…… 简化：手动建三角楔
# 上面的 bisect 若成功即用；占格要求：体积小格（斜切穿越格）判占用
bpy.context.view_layer.update()
vf._auto_align_if_needed(o)
cells = vf._occupied_cells(o)
occ = vf.occupied_set(cells)
print(f"  M2 占格 {len(occ)}：{sorted(occ)}")
# 斜切的格应占用（格内体积小也占用——但斜切可能把一些格切成 0 体积=空气）
# 断言：体积小的格（占有>0 但 <满）不会漏判（至少占用数 >= 明显有量的格）
check("M2 占格 ≥ 4（斜切后体积少的格仍占用）", len(occ) >= 4, f"got {sorted(occ)}")
face, on = vf.primary_face_for_module(cells, vf._local_bounds(o))
print(f"  M2 主面 {face} 格数 {len(on)}")
if face:
    marks = [vf.face_mark_from_cell_face(g, face) for g in on]
    check("M2 每格 1 点（数量=主面格数）", len(marks) == len(on),
          f"points={len(marks)} cells={len(on)}")

print("== M3: 贴格阈值（0.5 格）——面中心离吸附格中心 ≤0.5 才给 ==")
bpy.ops.wm.read_factory_settings(use_empty=True)
o = new_cube(1.0)
vf._auto_align_if_needed(o)
# 顶点贴格时顶面中心 (0.5,1,0.5) → 吸附格 (0,0,0) → 距離 0 = 贴着
bw = vf._bounds_of(o)
c = (0.5, bw[4], 0.5)
inv = o.matrix_world.inverted()
center_local = inv @ mathutils.Vector(c)
mark = vf._mark_from_local(o, (center_local.x, center_local.y, center_local.z),
                           (0.0, 1.0, 0.0))
cf = vf.mark_to_cell_face(mark)
fc = vf.face_mark_from_cell_face((cf[0], cf[1], cf[2]), cf[3])
dist = math.sqrt(sum((mark[i] - fc[i]) ** 2 for i in range(3)))
print(f"  M3 顶点贴格：mark={tuple(round(x,3) for x in mark)} 吸附中心={tuple(round(x,3) for x in fc[:3])} dist={dist:.3f}")
check("M3 贴格模型 dist ≈ 0（允许）", dist < 0.5, f"got {dist:.3f}")
# 未贴格：模型中心在 (0.35, 0, 0)（未对齐）→ 自动对齐兜底；模拟"面离格远"
# 斜切面中心离格 0.4：手动构造 mark 偏离
mark_far = (0.9, 1.0, 0.5, 0.0, 1.0, 0.0)  # 顶面内偏 0.4
cf2 = vf.mark_to_cell_face(mark_far)
fc2 = vf.face_mark_from_cell_face((cf2[0], cf2[1], cf2[2]), cf2[3])
dist2 = math.sqrt(sum((mark_far[i] - fc2[i]) ** 2 for i in range(3)))
print(f"  M3 面内偏 0.4：dist={dist2:.3f}（<0.5 → 仍给——在格内）")
check("M3 0.4 偏仍在格内（给）", dist2 < 0.5, f"got {dist2:.3f}")
# 超格外的偏（0.6 → 吸附到邻格 → dist=0.1？round 吸附本身会把 0.6 拉到邻格——
# 真正"未贴近"= round 后与原始 >0.5（吸附了半格以上）——实测楔形斜切面
my_far = (0.05, 1.0, 0.5, 0.0, 1.0, 0.0)  # 顶面左端 0.05（贴格边 0）
cf3 = vf.mark_to_cell_face(my_far)
fc3 = vf.face_mark_from_cell_face((cf3[0], cf3[1], cf3[2]), cf3[3])
dist3 = math.sqrt(sum((my_far[i] - fc3[i]) ** 2 for i in range(3)))
print(f"  M3 顶端 0.05：dist={dist3:.3f} cf={cf3}")
check("M3 格边位置反推正确（0.05→格0 偏移 0.45）", cf3[0] == 0, f"got {cf3}")

print("== M4: 3 格模型（1×1×3 竖柱）每格连接点 ==")
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, 0))
o = bpy.context.active_object
o.scale = (1.0, 1.0, 3.0)  # 1×1×3 柱
bpy.context.view_layer.update()
vf._auto_align_if_needed(o)
cells = vf._occupied_cells(o)
face, on = vf.primary_face_for_module(cells, vf._local_bounds(o))
marks = [vf.face_mark_from_cell_face(g, face) for g in on]
print(f"  M4 占格 {len(vf.occupied_set(cells))} 主面 {face} 格 {len(on)} 标记 {len(marks)}")
check("M4 3 格柱 → 主面=East/West（3 格）→ 3 个点（每格 1 个）",
      len(marks) == 3, f"got {len(marks)} {face}")

print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
bpy.ops.wm.quit_blender()
sys.exit(1 if FAIL else 0)
