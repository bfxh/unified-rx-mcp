# -*- coding: utf-8 -*-
"""v28：2026-08-23 用户最终定案语义探针。

1. gen_mp 默认 = 主面**中心 1 点**（不跟数量级涨——没点批量不涨）
2. face_expand_rings = 中心格→四周逐环（涟漪扩散，仅主面格）
3. 连接点**不随体积涨**（放大后默认仍 1 个；尺寸恒 1 米格面）
4. 缩小跟网格走（<1 米仍 1 米格）
"""
import sys
sys.path.insert(0, r"D:\开发\VoxelForge\tools\blender")
import bpy
from mathutils import Vector

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


def reset(scale, name):
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, 0))
    obj = bpy.context.active_object
    obj.scale = scale
    obj.location = (scale[0] / 2, scale[1] / 2, scale[2] / 2)
    obj.name = name
    bpy.context.view_layer.objects.active = obj
    bpy.ops.voxelforge.align_grid()
    bpy.ops.voxelforge.gen_mp()
    return obj


import voxelforge_connector as vf

# 1) 2×2×2 放大模型：默认只 1 个连接点（不跟数量级涨）
obj = reset((2.0, 2.0, 2.0), "c2x2x2")
marks = list(obj.get("vf_connect_points", []))
check("2×2×2 默认单点", len(marks) == 1,
      f"（标记数={len(marks)}，期望 1——没点批量不涨）")
m0 = marks[0]
check("默认单点贴顶面格", abs(m0[1] - 2.0) < 1e-3,
      f"（标记格中心 y={m0[1]}，期望 2.0=顶面格）")

# 2) 批量展开环序（4 格 Top：中心→四周，曼哈顿距离环）
# 起点 (0,2,0)：d0={起点} d1={(0,2,1),(1,2,0)} d2={(1,2,1)}——3 环
rings = vf.face_expand_rings("Top", [(0, 2, 0), (0, 2, 1), (1, 2, 0), (1, 2, 1)],
                             (0, 2, 0))
check("4 格环==3 层（方形角格）", len(rings) == 3,
      f"（ring={[len(r) for r in rings]}，期望 [1,2,1]）")
check("第 1 环={起点}", rings[0] == [(0, 2, 0)], f"（{rings[0]}）")
check("第 2 环=距离 1 两格", len(rings[1]) == 2, f"（{rings[1]}）")

# 3) 1 格模型：环=1（只有中心）
rings1 = vf.face_expand_rings("Top", [(0, 1, 0)], (0, 1, 0))
check("1 格 1 环", len(rings1) == 1 and rings1[0] == [(0, 1, 0)])

# 4) 缩小 0.5：默认 1 点 + 跟网格走（1 米格）
obj05 = reset((0.5, 0.5, 0.5), "c05")
marks05 = list(obj05.get("vf_connect_points", []))
check("0.5³ 默认单点", len(marks05) == 1, f"（{len(marks05)}）")
m05 = marks05[0]
check("0.5³ 连接点跟网格走（1 米格）", abs(m05[1] - 1.0) < 1e-3,
      f"（标记 y={m05[1]}，期望 1.0=1 米格面）")

# 5) 1×1×1 默认 1 点
obj2 = reset((1.0, 1.0, 1.0), "c1")
check("1×1×1 默认 1 点", len(list(obj2.get("vf_connect_points", []))) == 1)

# 6) 3×2 顶面（6 格）环序
m3 = [(0, 2, 0), (0, 2, 1), (1, 2, 0), (1, 2, 1), (2, 2, 0), (2, 2, 1)]
c3 = vf.face_center_cell("Top", m3)
rings3 = vf.face_expand_rings("Top", m3, c3)
check("3×2 中心在面内", c3 in m3, f"（{c3}）")
check("3×2 环序覆盖全部 6 格", sum(len(r) for r in rings3) == 6,
      f"（{len(rings3)} 环）")

print(f"v28 结果: {ok} 通过 / {fail} 失败")
sys.exit(1 if fail else 0)
