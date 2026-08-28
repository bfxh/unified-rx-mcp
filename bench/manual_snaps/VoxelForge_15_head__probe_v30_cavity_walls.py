# -*- coding: utf-8 -*-
"""v30：空心壳内面（法向指向腔格）暴露判定最终验证。

用户截图（透明大立方体内部两个竖绿面）= 修复前空心壳**侧壁内面**
（East/West，法向指向空腔）被判暴露 → 绿面"透"在模型内部。
本探针验证修复（is_exposed_face + bounds）后：24 个真实内面全不暴露。
"""
import sys
sys.path.insert(0, r"D:\开发\VoxelForge\tools\blender")
import voxelforge_connector as vf

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


# 4³ 挖 2³ 腔（腔格 1..2）
cells = []
for x in range(4):
    for y in range(4):
        for z in range(4):
            inner = 1 <= x <= 2 and 1 <= y <= 2 and 1 <= z <= 2
            cells.append((x, y, z, not inner))
bounds = (0, 0, 0, 4, 4, 4)
occ = vf.occupied_set(cells)

# 真实内面 = 占格外壳格 + 相邻腔格 + 法向朝腔
real_inner = []
for g in occ:
    for face, (dx, dy, dz) in vf.FACE_OFFSETS.items():
        nb = (g[0] + dx, g[1] + dy, g[2] + dz)
        if nb not in occ and 1 <= nb[0] <= 2 and 1 <= nb[1] <= 2 and 1 <= nb[2] <= 2:
            real_inner.append((g[0], g[1], g[2], face))
check("内面总数 24", len(real_inner) == 24, f"（got {len(real_inner)}）")
bad = [w for w in real_inner if vf.is_exposed_face(w, cells, bounds)]
check("内面误暴露 0", len(bad) == 0, f"（got {len(bad)}: {bad[:3]}）")
# 内面无 bounds 时旧逻辑会误暴露（原 bug 复现）
bad_old = [w for w in real_inner if vf.is_exposed_face(w, cells)]
check("旧逻辑误暴露 >0（原 bug 复现）", len(bad_old) > 0,
      f"（{len(bad_old)}——证明修复必要）")
# 外表面全暴露
outer_all = True
for g in occ:
    for face, (dx, dy, dz) in vf.FACE_OFFSETS.items():
        nb = (g[0] + dx, g[1] + dy, g[2] + dz)
        if nb not in occ and not (1 <= nb[0] <= 2 and 1 <= nb[1] <= 2 and 1 <= nb[2] <= 2):
            if not vf.is_exposed_face((g[0], g[1], g[2], face), cells, bounds):
                outer_all = False
check("外表面全暴露", outer_all)

# 主面=纯外表面（无内面混入）：主面格的法向朝向腔格才算混入
face, cells_on = vf.primary_face_for_module(cells, bounds)
dx, dy, dz = vf.FACE_OFFSETS[face]
mixed = [c for c in cells_on
         if (c[0] + dx, c[1] + dy, c[2] + dz) not in occ and
         1 <= c[0] + dx <= 2 and 1 <= c[1] + dy <= 2 and 1 <= c[2] + dz <= 2]
check("主面无内面混入", len(mixed) == 0,
      f"（主面 {face} {len(cells_on)} 格，混入 {len(mixed)}）")

print(f"v30 结果: {ok} 通过 / {fail} 失败")
sys.exit(1 if fail else 0)
