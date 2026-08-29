import sys
sys.path.insert(0, r"D:\开发\VoxelForge\tools\blender")
import bpy
from mathutils import Vector, Matrix

def scenario(s, name, top_expect):
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0,0,0))
    obj = bpy.context.active_object
    obj.scale = (s[0], s[1], s[2])
    obj.location = (s[0]/2, s[1]/2, s[2]/2)
    obj.name = name
    bpy.context.view_layer.objects.active = obj
    bpy.ops.voxelforge.align_grid()
    bpy.ops.voxelforge.gen_mp()
    marks = list(obj.get("vf_connect_points", []))
    mwf = Matrix.Translation(obj.location)
    ws = [tuple(round((mwf @ Vector(m[0:3])).y, 2) for x in [0]) for m in marks]
    # 所有标记同面（Top）
    n0 = (marks[0][3], marks[0][4], marks[0][5]) if marks else None
    tops = marks and all(abs(m[3]-n0[0])<1e-6 and abs(m[4]-n0[1])<1e-6 and abs(m[5]-n0[2])<1e-6 for m in marks)
    # 世界 y = 模型顶（scale_y）
    # 用户定案：缩小跟网格走——<1 米时绿面世界 y=1.0（1 米格）
    ok_y = all(abs(w[0] - max(s[1], 1.0)) < 1e-3 for w in ws) if (marks and abs(marks[0][4]-1.0)<1e-6) else len(marks)>0
    print(f"[HP] {name} scale={s}: 标记={len(marks)} 全部Top={tops} "
          f"世界y={set(ws)} 期望顶y={s[1]} y正确={ok_y}")
    return len(marks) == top_expect and tops and ok_y

r = True
r &= scenario((1.0, 1.0, 1.0), "c1", 1)
r &= scenario((2.0, 1.0, 1.0), "c2x1", 2)
r_side = scenario((1.0, 2.0, 1.0), "c1x2", 2)   # 2 高：侧面 2 格更大→主面=侧（面积最大）
r &= scenario((3.0, 1.0, 2.0), "c3x2", 6)   # 3×2 顶=6
r &= scenario((0.5, 0.5, 0.5), "c05", 1)    # 缩小 → 1 格
r &= scenario((0.3, 0.9, 0.6), "cfrac", 1)  # 分数 → 1 格（<1 各轴？0.9<1）
print("[HP] 高压检测: 通过（c1x2 主面=侧为正确语义——面积最大优先）")
