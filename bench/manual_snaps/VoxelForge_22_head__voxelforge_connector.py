# -*- coding: utf-8 -*-
"""
VoxelForge Blender 模块化连接插件 v2.1
======================================
（2026-08-18 重建：GAME_DEV_TASKS.md 记录的原文件缺失，按 docs/MODELING_PLUGIN.md v2 规格重建）

功能闭环：任意模型 → 网格对齐 → 连接点标注（含设计性数量规则）→ 一键导出 GLB + RON。

核心规范（建模铁律，与游戏 nexus_core 严格对齐）：
- 1 格 = 1 米；模块局部 +Y=上、-Z=前（North）；对象名 <corp>_<name>；id = "<corp>.<name>"
- 连接点 = 面标记（vf_connect_points：点击面/自动生成，默认格面中间；
  面内调整不超格；只对外面有效）
- 导出 RON schema v4（ModuleDef：id/name/corp/category/mass/hp/shape/mount_points/components/model_path/tags）

连接点设计性数量规则（用户 2026-08-18 定案）：
- 默认：每一个格子 6 面全开可拼（full_6）
- 特殊模块（装饰件/端件/功能件）：只保留 2-3 个连接点（pair_2 = 对向链式 / tri_3 = T 形）
- 规则由 RON 数据驱动（mount_points 显式列表）——插件负责按方案生成，游戏只认列表

用法：
- GUI：3D 视图 → N 面板 → VoxelForge Tab
- 无头批量导出：blender --background --python voxelforge_connector.py -- --export-all <dir> [--corp <corp>]
- 单测（不依赖 Blender）：python tools/blender/test_voxelforge_connector.py
"""

import os
import sys

# ════════════════════════════════════════════════════════════════════
# 一、核心纯函数（不依赖 bpy——headless 可测）
# ════════════════════════════════════════════════════════════════════

# 面朝向 ↔ 轴方向（模块局部坐标系：+Y=上 Top、-Z=前 North）
FACE_AXES = [
    ("Top", (0, 1, 0)),
    ("Bottom", (0, -1, 0)),
    ("North", (0, 0, -1)),
    ("South", (0, 0, 1)),
    ("East", (1, 0, 0)),
    ("West", (-1, 0, 0)),
]
FACES = [f for f, _ in FACE_AXES]

# 连接点数量方案（设计性规则）：
#   full_6 = 每格 6 面全开（默认——"每一个格子都可以拼接"）
#   pair_2 = 每格只 2 个对向连接点（链式：车头尾/走廊两端）
#   tri_3  = 每格 3 个 T 形连接点（主体 + 单侧分支——分岔件/端件）
SCHEMES = {
    "full_6": FACES,
    "pair_2": ["North", "South"],
    "tri_3": ["North", "South", "Top"],
}

# face → 轴偏移（暴露面判定用：格 (x,y,z) 的 face 可连 ⟺ 相邻格不在占用集）
FACE_OFFSETS = dict(FACE_AXES)


def occupied_set(cells):
    """占用格列表 → 占用格集合（兼容两种格式：[(cx,cy,cz,occupied)] 或
    [(cx,cy,cz)]；occupied=False 的空气格自动跳过）。"""
    out = set()
    for c in cells:
        if len(c) >= 4 and not c[3]:
            continue
        out.add((int(c[0]), int(c[1]), int(c[2])))
    return out


def primary_face_for_module(cells, bounds=None, external_air=None):
    """模块主连接面（2026-08-22 用户："连接点一面就可以，连接点只是对一面
    有效果，一个直接就是一面就可以"——一个模块只有**一个**连接面）。

    选择逻辑：暴露面（exposed）面积最大者；平手时 Top > Bottom > 各侧
    （同面积 Top 优先——模块默认朝上拼接）。
    bounds（格空间 bbox）给定时排除空腔壁面（2026-08-23：挖空盒子
    内壁不是连接面——连接点只对外表面）；external_air 为预计算的
    空气连通集（v31：浅凹槽面不再误判内壁；None 时内部计算）。
    返回 (face, [(cell_x, cell_y, cell_z), ...])——该面下所有暴露格；
    无暴露面返回 (None, [])。
    """
    occ = occupied_set(cells)
    if bounds is not None and external_air is None:
        external_air = external_air_cells(cells, bounds)  # 一次预计算（v31）
    best_face = None
    best_cells = []
    best_count = -1
    # 优先级（同面积平手裁决）：Top > Bottom > North > South > East > West
    priority = {"Top": 0, "Bottom": 1, "North": 2, "South": 3,
                "East": 4, "West": 5}
    for face, (dx, dy, dz) in FACE_OFFSETS.items():
        cells_on = []
        for (cx, cy, cz) in occ:
            if (cx + dx, cy + dy, cz + dz) in occ:
                continue  # 非暴露（埋藏）
            if bounds is not None and not is_exposed_face(
                    (cx, cy, cz, face), cells, bounds, external_air, occ):
                continue  # 空腔壁面（法向指向模型内部）——不入选
            cells_on.append((cx, cy, cz))
        if not cells_on:
            continue
        if len(cells_on) > best_count:
            best_count = len(cells_on)
            best_face = face
            best_cells = sorted(cells_on)
        elif len(cells_on) == best_count and best_face is not None:
            # 同面积平手：优先级裁决
            if priority.get(face, 9) < priority.get(best_face, 9):
                best_face = face
                best_cells = sorted(cells_on)
    return (best_face, best_cells)


def exposed_faces(cells):
    """占用格集合 → 暴露面集合 {(cell, face)}。

    复杂多边形形状（L 形/斜切/挖空）核心：格 (x,y,z) 的 face 方向可连接
    ⟺ 该方向的相邻格不在占用集（没有被别的占用格挡住=暴露在外）。
    埋在模型内部/被相邻格挡住的连接点自动消失——游戏里根本接不上。
    """
    occ = occupied_set(cells)
    out = set()
    for (cx, cy, cz) in occ:
        for face, (dx, dy, dz) in FACE_OFFSETS.items():
            nb = (cx + dx, cy + dy, cz + dz)
            if nb not in occ:
                out.add(((cx, cy, cz), face))
    return out


def mount_points_for_occupied(cells, scheme="exposed", strength=100.0, accepts="Any"):
    """按实际占用格生成连接点（复杂形状用——空气格跳过，只在实际占用格）。

    - exposed：仅暴露面（默认——复杂形状只在外表面生成连接点）
    - full_6 / pair_2 / tri_3：同方案语义但只落在占用格（空气格跳过）
    返回 [(cell_x, cell_y, cell_z, face, strength, accepts)]。
    """
    occ = occupied_set(cells)
    faces = SCHEMES[scheme] if scheme != "exposed" else [f for f, _ in FACE_AXES]
    out = []
    for (cx, cy, cz) in sorted(occ):
        for face in faces:
            if scheme == "exposed":
                dx, dy, dz = FACE_OFFSETS[face]
                if (cx + dx, cy + dy, cz + dz) in occ:
                    continue  # 相邻格占用=非暴露面，跳过
            out.append((cx, cy, cz, face, strength, accepts))
    return out


def occ_outline_edges(occ_cells, mw=None):
    """占用格集合 → 整体外框 12 条边的顶点对（按最大边缘）。

    用户 2026-08-22："那个框框是按最大的边缘来算的，不要给我搞这么多
    条缝 或者线太卡了"——占用格逐格画 12 条边/格，大模块（几十上百格）
    会画出上千条线卡死；改为只画占用区域包围盒外框（min 格 → max+1），
    线条数恒 12 条（24 顶点）。空心格仍逐格（数量少，可放模块提示）。
    mw：对象矩阵（可选）——格线是局部坐标，渲染需转世界（None=纯局部，
    headless 可测）。
    """
    if not occ_cells:
        return []
    xs = [int(c[0]) for c in occ_cells]
    ys = [int(c[1]) for c in occ_cells]
    zs = [int(c[2]) for c in occ_cells]
    x0, y0, z0 = float(min(xs)), float(min(ys)), float(min(zs))
    x1, y1, z1 = float(max(xs) + 1), float(max(ys) + 1), float(max(zs) + 1)

    def _t(x, y, z):
        if mw is None:
            return [x, y, z]
        from mathutils import Vector  # 延迟导入——顶层纯函数区不依赖 bpy
        v = mw @ Vector((x, y, z))
        return [v.x, v.y, v.z]

    out = []
    out.append(_t(x0, y0, z0)); out.append(_t(x1, y0, z0))
    out.append(_t(x1, y0, z0)); out.append(_t(x1, y1, z0))
    out.append(_t(x1, y1, z0)); out.append(_t(x0, y1, z0))
    out.append(_t(x0, y1, z0)); out.append(_t(x0, y0, z0))
    out.append(_t(x0, y0, z1)); out.append(_t(x1, y0, z1))
    out.append(_t(x1, y0, z1)); out.append(_t(x1, y1, z1))
    out.append(_t(x1, y1, z1)); out.append(_t(x0, y1, z1))
    out.append(_t(x0, y1, z1)); out.append(_t(x0, y0, z1))
    out.append(_t(x0, y0, z0)); out.append(_t(x0, y0, z1))
    out.append(_t(x1, y0, z0)); out.append(_t(x1, y0, z1))
    out.append(_t(x1, y1, z0)); out.append(_t(x1, y1, z1))
    out.append(_t(x0, y1, z0)); out.append(_t(x0, y1, z1))
    return out


def face_mark_from_geometry(center_local, normal_local):
    """局部面中心 + 法线 → 连接面标记 (mx, my, mz, nx, ny, nz)。

    吸附到网格格（用户 2026-08-19："连接始终是对着网格来搞的，不是对着
    模型"）：cell = round(中心 - 法向×0.5 - 0.5)；标记位置 = 格中心 + 法向×0.5。
    与 FaceConnectToggle 公式一致——纯 tuple 数学（headless 可测）。
    """
    nx, ny, nz = normal_local
    cell_x = round(center_local[0] - nx * 0.5 - 0.5)
    cell_y = round(center_local[1] - ny * 0.5 - 0.5)
    cell_z = round(center_local[2] - nz * 0.5 - 0.5)
    return (cell_x + 0.5 + nx * 0.5,
            cell_y + 0.5 + ny * 0.5,
            cell_z + 0.5 + nz * 0.5,
            nx, ny, nz)


def external_air_cells(cells, bounds):
    """包围盒内空气格中**连通到包围盒外**的集合（BFS）——空腔壁精确判定。

    2026-08-23 v31 修复：旧 is_exposed_face 用"面中心沿法向外推 0.3 格
    仍在 bbox 内 → 内壁"判据，**浅凹槽**（L 形缺角/凹形开槽）被误判为
    内壁（用户点面被拒"内面"、批量标记漏面、渲染绿面缺失）。精确判据：
    暴露面法向相邻格（空气格）须能**连通到包围盒外**——凹槽连通外部
    → 暴露面；空腔（被封死）→ 内壁。
    bounds：格空间 bbox（_local_bounds 值）；返回 frozenset{空气格}。
    """
    import math
    occ = occupied_set(cells)
    x0 = math.floor(bounds[0]); y0 = math.floor(bounds[1]); z0 = math.floor(bounds[2])
    x1 = math.ceil(bounds[3]); y1 = math.ceil(bounds[4]); z1 = math.ceil(bounds[5])
    air = set()
    for x in range(x0, x1):
        for y in range(y0, y1):
            for z in range(z0, z1):
                if (x, y, z) not in occ:
                    air.add((x, y, z))
    if not air:
        return frozenset()
    # 起点：与包围盒边界接触的空气格（外部空气从边界渗入）
    frontier = [(x, y, z) for (x, y, z) in air
                if x == x0 or x == x1 - 1 or y == y0 or y == y1 - 1
                or z == z0 or z == z1 - 1]
    seen = set(frontier)
    while frontier:
        nf = []
        for (x, y, z) in frontier:
            for (dx, dy, dz) in ((1, 0, 0), (-1, 0, 0), (0, 1, 0),
                                 (0, -1, 0), (0, 0, 1), (0, 0, -1)):
                nb = (x + dx, y + dy, z + dz)
                if nb in air and nb not in seen:
                    seen.add(nb)
                    nf.append(nb)
        frontier = nf
    return frozenset(seen)


def is_exposed_face(cell_face, cells, bounds=None, external_air=None, occ=None):
    """(cx,cy,cz,face) + 占用格列表 → 是否外面（该方向无相邻占用格）。

    用户 2026-08-21 定案："只对各种外面是有效果的"——连接点只能设在外
    表面（埋藏面/内面禁止）。operator 点击限制 + 渲染过滤共用。

    2026-08-23 修复（用户截图：透明大立方体**内部**出现绿面）：
    bounds（格空间 bbox）给定时，**空腔壁面**（法向指向模型内部空间，
    如挖空盒子的内壁）也判为不暴露——"空心可以放模块 ≠ 空腔内壁可
    标连接点"，连接点只对外表面。
    2026-08-23 v31 修复：判据从"面中心沿法向外推 0.3 格仍在 bbox 内"
    升级为**空气连通性**（external_air_cells）：法向相邻空气格须连通
    包围盒外（浅凹槽不再误判）；法向相邻格**在 bbox 外**=外表面直通外部
    （实心满格模型顶面/底面——A4 实锤：air 为空时仍须暴露）。
    external_air：预计算的空气连通集（高频路径由调用方缓存传入；None
    时内部计算——低频路径用）。
    occ：预构建的占用格集合（v31d 性能：旧实现每次调用重建 occupied_set
    O(N)——primary_face_for_module 的 6×N 格循环变成 O(6N²)（20³ 实测
    3.5s），draw 对 N 个标记也每帧 O(N²)；调用方传预构建 occ 后全部 O(N)。
    """
    import math
    cx, cy, cz, face = cell_face
    dx, dy, dz = FACE_OFFSETS.get(face, (0.0, 0.0, 0.0))
    if occ is None:
        occ = occupied_set(cells)  # 统一构建（v31：3/4 元组 cells 均兼容）
    nb = (cx + dx, cy + dy, cz + dz)
    if nb in occ:
        return False  # 埋藏（相邻有占用格）
    if bounds is not None:
        if external_air is None:
            external_air = external_air_cells(cells, bounds)
        if nb in external_air:
            return True  # 法向相邻格连通外部（含凹槽）
        x0 = math.floor(bounds[0]); y0 = math.floor(bounds[1]); z0 = math.floor(bounds[2])
        x1 = math.ceil(bounds[3]); y1 = math.ceil(bounds[4]); z1 = math.ceil(bounds[5])
        if not (x0 <= nb[0] < x1 and y0 <= nb[1] < y1 and z0 <= nb[2] < z1):
            return True  # 法向相邻格在包围盒外 = 外表面（直通外部）
        return False  # 空腔壁（法向指向被封锁的内部空间）
    return True


def face_mark_world_center(mark, obj_local_bounds):
    """面标记 → 模型外表面实际中心（2026-08-22 用户"绿面偏小/偏移"修复）。

    格系统（1 格 = 1 米，向上取整）在非单位缩放模型上会把 1.3 米划成
    2 格，绿面按格画 1×1 会超出/偏移模型表面。本函数把绿面钉到模型
    **实际外表面**：中心 = bbox 该面中心（法向轴取 bbox 边，切向取中间）。
    obj_local_bounds = _local_bounds(obj)（缩放空间 bbox）。
    返回 (mx, my, mz, 法向, su, sv)——面中心 + 半宽/半高（按 bbox 尺寸）。
    """
    nx, ny, nz = mark[3], mark[4], mark[5]
    x0, y0, z0, x1, y1, z1 = obj_local_bounds
    if abs(nx) > 0.5:  # East/West
        cx = x1 if nx > 0 else x0
        return (cx, (y0 + y1) * 0.5, (z0 + z1) * 0.5, (nx, ny, nz),
                (y1 - y0) * 0.5, (z1 - z0) * 0.5)
    if abs(ny) > 0.5:  # Top/Bottom
        cy = y1 if ny > 0 else y0
        return ((x0 + x1) * 0.5, cy, (z0 + z1) * 0.5, (nx, ny, nz),
                (x1 - x0) * 0.5, (z1 - z0) * 0.5)
    # North/South
    cz = z1 if nz > 0 else z0
    return ((x0 + x1) * 0.5, (y0 + y1) * 0.5, cz, (nx, ny, nz),
            (x1 - x0) * 0.5, (y1 - y0) * 0.5)


def face_center_cell(face, cells_on):
    """主面中心格（2026-08-23 用户："哪一个是最先没被处理的，就最先
    那个就按那个来启动"——批量展开起点 = 面内格集合质心最近的格）。

    cells_on 是该暴露面下的占用格列表。返回 (cx, cy, cz)。
    """
    if not cells_on:
        return None
    n = len(cells_on)
    mx = sum(c[0] for c in cells_on) / n
    my = sum(c[1] for c in cells_on) / n
    mz = sum(c[2] for c in cells_on) / n
    best = min(cells_on, key=lambda c: ((c[0]-mx)**2 + (c[1]-my)**2 + (c[2]-mz)**2))
    return best


def face_expand_rings(face, cells, center):
    """批量展开顺序（2026-08-23 用户："按中心点来启动，向四周展开，
    但只是限于一面"）：从 center 按曼哈顿距离分环（涟漪扩散）。

    返回 [[格, ...], [格, ...], ...]——每环一组，动画逐环添加；
    环内按 (x,y,z) 稳定排序；center 自己在第一环（最先被处理）。
    """
    import math
    if not cells:
        return []
    cx, cy, cz = center
    dists = []
    for g in cells:
        d = abs(g[0] - cx) + abs(g[1] - cy) + abs(g[2] - cz)
        dists.append((d, g[0], g[1], g[2], g))
    dists.sort()
    rings = []
    cur = None
    for d, _, _, _, g in dists:
        if cur is None or d != cur:
            rings.append([g])
            cur = d
        elif rings:
            rings[-1].append(g)
    return rings


def merge_face_marks(marks, tolerance=0.05):
    """同格同向面标记合并为一个（复杂模型一格常被多个小面覆盖——斜切/
    细分曲面批量标记后去重）。方向按法线容差比较，位置按格容差比较。
    v31i 性能：旧实现双层循环 O(N²)（批量展开/400 格 = 16 万次比较——
    用户"所有东西延迟太大"）；改为坐标量化 set 去重 O(N)。"""
    out = []
    seen = set()
    key = lambda m: (round(m[0] / tolerance), round(m[1] / tolerance),
                     round(m[2] / tolerance), round(m[3] / tolerance),
                     round(m[4] / tolerance), round(m[5] / tolerance))
    for m in marks:
        k = key(m)
        if k not in seen:
            seen.add(k)
            out.append(m)
    return out


def merge_adjacent_face_marks(marks):
    """相邻同向面标记合并为整体面区域（2026-08-22 用户："连接点才这么小"）。

    每格一个连接点的**数据不变**（导出仍逐格），本函数只服务渲染：
    同一朝向且格相邻（面内相邻）的标记合并成矩形区域——2×1 顶面渲染为
    一个 2×1 整面，不是两个 1×1 小面。
    返回 [(face, (u0, v0, u1, v1), (nx, ny, nz), anchor_cell)]：
    u/v 为面内正交轴上的格坐标，区域为格边界 [u0..u1]×[v0..v1]（格）；
    anchor_cell = 区域内一个格的 (cx, cy, cz)（渲染定位锚——避免二次扫描，
    大模型上千矩形×上千标记的 O(n²) 扫描会卡 draw）。
    L 形/复杂形状做贪心行分解（多个矩形——绝不用包围盒，避免盖住空气）。
    """
    # 面内正交轴（与渲染 u/v 同约定：优先用不平行于法向的轴）
    def _face_axes(n):
        if abs(n[2]) < 0.9:
            u = (0.0, 0.0, 1.0)
        else:
            u = (1.0, 0.0, 0.0)
        ux, uy, uz = u
        # u = cross(u0, n) 归一化
        cu = (uy * n[2] - uz * n[1], uz * n[0] - ux * n[2], ux * n[1] - uy * n[0])
        cu_l = math.sqrt(cu[0] ** 2 + cu[1] ** 2 + cu[2] ** 2) or 1.0
        cu = (cu[0] / cu_l, cu[1] / cu_l, cu[2] / cu_l)
        cv = (n[1] * cu[2] - n[2] * cu[1],
              n[2] * cu[0] - n[0] * cu[2],
              n[0] * cu[1] - n[1] * cu[0])
        return cu, cv

    import math
    out = []
    # 按 face 分组
    by_face = {}
    for m in marks:
        cf = mark_to_cell_face(m)
        by_face.setdefault(cf[3], []).append((m, cf))
    for face, items in by_face.items():
        n = (0.0, 0.0, 0.0)
        cu, cv = None, None
        for m, _cf in items:
            nl = math.sqrt(m[3] ** 2 + m[4] ** 2 + m[5] ** 2) or 1.0
            n = (m[3] / nl, m[4] / nl, m[5] / nl)
            break
        if n == (0.0, 0.0, 0.0):
            continue
        cu, cv = _face_axes(n)
        # 每个标记 → 面内格坐标（cell 的 u/v 投影，取整）；anchor = 该格的 cell
        cells = {}
        for m, cf in items:
            u = round(cu[0] * cf[0] + cu[1] * cf[1] + cu[2] * cf[2])
            v = round(cv[0] * cf[0] + cv[1] * cf[1] + cv[2] * cf[2])
            cells[(u, v)] = (cf[0], cf[1], cf[2])
        # 贪心矩形分解：逐格向右再向下扩展（L 形→2 矩形，绝不盖空气格）
        rects = []
        used = set()
        for (u, v) in sorted(cells):
            if (u, v) in used:
                continue
            # 向右扩展
            u1 = u
            while (u1 + 1, v) in cells and (u1 + 1, v) not in used:
                u1 += 1
            # 向下扩展（整行可用才扩）
            v1 = v
            while True:
                if all((uu, v1 + 1) in cells and (uu, v1 + 1) not in used
                       for uu in range(u, u1 + 1)):
                    v1 += 1
                else:
                    break
            for uu in range(u, u1 + 1):
                for vv in range(v, v1 + 1):
                    used.add((uu, vv))
            rects.append((face, (u, v, u1 + 1, v1 + 1), n, cells[(u, v)]))
        out.extend(rects)
    return out


def face_from_normal(n):
    """面法线 → 面朝向（模块局部坐标约定：+Y=Top、-Y=Bottom、-Z=North、
    +Z=South、+X=East、-X=West——与游戏 Face 枚举一致）。"""
    x, y, z = n
    m = max(abs(x), abs(y), abs(z))
    if abs(y) == m:
        return "Top" if y > 0 else "Bottom"
    if abs(z) == m:
        return "South" if z > 0 else "North"
    return "East" if x > 0 else "West"


def mark_to_cell_face(mark):
    """连接面标记 (mx,my,mz,nx,ny,nz) → (cell, face)（校验逆运算）。

    与 face_mark_from_geometry 互逆：cell = round(mark - 法向×0.5 - 0.5)。
    """
    mx, my, mz, nx, ny, nz = mark
    return (round(mx - nx * 0.5 - 0.5),
            round(my - ny * 0.5 - 0.5),
            round(mz - nz * 0.5 - 0.5),
            face_from_normal((nx, ny, nz)))


def face_mark_from_cell_face(cell, face):
    """cell + face → 连接面标记 (mx,my,mz,nx,ny,nz)。

    位置 = 格面中心（点击面产生的标记同款位置——默认在中间）；
    法线 = 该面法线。自动生成 exposed 用（与点击面产生的标记同构）。
    """
    cx, cy, cz = cell
    if face == "East":
        return (cx + 1.0, cy + 0.5, cz + 0.5, 1.0, 0.0, 0.0)
    if face == "West":
        return (cx, cy + 0.5, cz + 0.5, -1.0, 0.0, 0.0)
    if face == "Top":
        return (cx + 0.5, cy + 1.0, cz + 0.5, 0.0, 1.0, 0.0)
    if face == "Bottom":
        return (cx + 0.5, cy, cz + 0.5, 0.0, -1.0, 0.0)
    if face == "South":
        return (cx + 0.5, cy + 0.5, cz + 1.0, 0.0, 0.0, 1.0)
    return (cx + 0.5, cy + 0.5, cz, 0.0, 0.0, -1.0)


def clamp_mark_to_face(mark, cells, cell=None):
    """连接面标记钳制到所在格子的面范围内（2026-08-21 面调整规则）：

    - 只对外面有效：标记所在格必须是占用格（不在占用集 → 标记不变，
      由校验/修复处理）
    - 不超格子/不低于格子：切向钳制在 [格边界, 格边界+1]
    - 法向固定：不会离开这一面；作用只在这一面（只改本标记）
    cell：钳制基准格（面调整拖动时传**标记初始 cell**——位置移动后
    反推会错位）；缺省时从标记反推（浮空标记 → 不动）。
    返回钳制后的标记 (mx,my,mz,nx,ny,nz)。
    """
    mx, my, mz, nx, ny, nz = mark
    if cell is None:
        cf = mark_to_cell_face(mark)
    else:
        cf = (int(cell[0]), int(cell[1]), int(cell[2]),
              mark_to_cell_face(mark)[3])
    occ = occupied_set(cells)
    if (cf[0], cf[1], cf[2]) not in occ:
        return mark  # 非占用格（浮空标记）——不动，交给校验/修复
    face = cf[3]
    cx, cy, cz = cf[0], cf[1], cf[2]
    if face == "East":
        return (cx + 1.0, min(max(my, cy), cy + 1.0), min(max(mz, cz), cz + 1.0), nx, ny, nz)
    if face == "West":
        return (cx, min(max(my, cy), cy + 1.0), min(max(mz, cz), cz + 1.0), nx, ny, nz)
    if face == "Top":
        return (min(max(mx, cx), cx + 1.0), cy + 1.0, min(max(mz, cz), cz + 1.0), nx, ny, nz)
    if face == "Bottom":
        return (min(max(mx, cx), cx + 1.0), cy, min(max(mz, cz), cz + 1.0), nx, ny, nz)
    if face == "South":
        return (min(max(mx, cx), cx + 1.0), min(max(my, cy), cy + 1.0), cz + 1.0, nx, ny, nz)
    return (min(max(mx, cx), cx + 1.0), min(max(my, cy), cy + 1.0), cz, nx, ny, nz)


def validate_mp_against_cells(mount_points, cells):
    """连接点 vs 占用格三类校验（复杂形状排查）：

    返回 [(kind, message)]：
    - float    浮空：cell 不在占用集（空气格/悬空）
    - buried   埋内部：cell 在占用集但该面相邻格也占用（接不上）
    - duplicate 冗余：同 cell 同 face 重复
    """
    occ = occupied_set(cells)
    seen = set()
    errors = []
    for mp in mount_points:
        cx, cy, cz, face = mp[0], mp[1], mp[2], mp[3]
        key = (cx, cy, cz, face)
        if key in seen:
            errors.append(("duplicate", f"cell ({cx},{cy},{cz}) {face} 重复"))
            continue
        seen.add(key)
        if (cx, cy, cz) not in occ:
            errors.append(("float", f"cell ({cx},{cy},{cz}) 不在占用格（浮空/空气格）"))
            continue
        dx, dy, dz = FACE_OFFSETS.get(face, (0, 0, 0))
        if (cx + dx, cy + dy, cz + dz) in occ:
            errors.append(("buried", f"cell ({cx},{cy},{cz}) {face} 被相邻格挡住（埋内部）"))
    return errors


def dims_from_bounds(bounds):
    """包围盒 → 格数 dims（用户 2026-08-18 规则："一米几→两格，每个格子按
    具体情况自动开搞"——每轴向上取整，最小 1 格）。

    - 1.0 米 → 1 格（正好一格）
    - 1.2 / 1.9 米 → 2 格（超过 1 米按 2 格算）
    - 2.3 米 → 3 格；0.8 米 → 1 格（最小 1）
    - 每轴独立；-1e-6 容差防浮点 1.0000001 误升
    """
    import math
    min_x, min_y, min_z, max_x, max_y, max_z = bounds
    dx = max(int(math.ceil(max_x - min_x - 1e-6)), 1)
    dy = max(int(math.ceil(max_y - min_y - 1e-6)), 1)
    dz = max(int(math.ceil(max_z - min_z - 1e-6)), 1)
    return (dx, dy, dz)


def align_offset_to_grid(bounds):
    """包围盒最小角向下取整 → 平移量 (tx, ty, tz)（角落落回整数格）。"""
    min_x, min_y, min_z, _, _, _ = bounds
    return (round(int(min_x) - min_x, 6),
            round(int(min_y) - min_y, 6),
            round(int(min_z) - min_z, 6))


def align_center_offset(bounds):
    """包围盒中心平移到最近整数格 → 平移量。"""
    min_x, min_y, min_z, max_x, max_y, max_z = bounds
    cx = (min_x + max_x) / 2.0
    cy = (min_y + max_y) / 2.0
    cz = (min_z + max_z) / 2.0
    return (round(cx) - cx, round(cy) - cy, round(cz) - cz)


def scale_to_dims(bounds, target):
    """按格数缩放：当前包围盒 → target (dx, dy, dz) 米的缩放系数 (sx, sy, sz)。"""
    cur = dims_from_bounds(bounds)
    return (target[0] / cur[0], target[1] / cur[1], target[2] / cur[2])


def _ron_f32(v):
    """RON 浮点格式化（去掉多余的 .0——与游戏资产风格一致但保留精度）。"""
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return s or "0"


def mount_point_to_ron(mp):
    """单个 MountPoint → RON 片段。

    mp = (cell_x, cell_y, cell_z, face, strength, accepts, layer, offset, align)
    """
    (cx, cy, cz, face, strength, accepts, layer, offset, align) = mp
    off = f"({_ron_f32(offset[0])}, {_ron_f32(offset[1])}, {_ron_f32(offset[2])})"
    return (
        f"        MountPoint(cell: ({cx}, {cy}, {cz}), face: {face}, "
        f"accepts: {accepts}, strength: {_ron_f32(strength)}, "
        f"layer: {layer}, offset: {off}, align: {'true' if align else 'false'}),"
    )


CATEGORY_HINTS = {
    "wheel": "Wheel",
    "tire": "Wheel",
    "engine": "Engine",
    "fuel": "FuelTank",
    "tank": "FuelTank",
    "cab": "Cab",
    "cockpit": "Cab",
    "seat": "Cab",
    "weapon": "Weapon",
    "gun": "Weapon",
    "turret": "Weapon",
    "manufacturer": "Manufacturer",
    "atom": "AtomCompressor",
    "compressor": "AtomCompressor",
    "conveyor": "Conveyor",
    "light": "Light",
    "lamp": "Light",
    "structure": "Structure",
    "block": "Structure",
    "frame": "Structure",
    "panel": "Structure",
    "armor": "Structure",
}


def category_from_name(name):
    """对象名 → Category（前缀匹配；默认 Structure）。"""
    lower = name.lower()
    for key, cat in CATEGORY_HINTS.items():
        if key in lower:
            return cat
    return "Structure"


def export_module_ron(module_id, name, corp, category, mass, hp, dims,
                      mount_points, model_path, tags=None, components=None):
    """导出完整 ModuleDef RON（schema v4——与 nexus_core 字段严格对齐）。

    mount_points：[(cell_x, cell_y, cell_z, face, strength, accepts, layer, offset, align)]
    """
    tags = tags or []
    components = components or []
    lines = [
        "ModuleDef(",
        f"    schema_version: 4,",
        f"    id: \"{module_id}\",",
        f"    name: \"{name}\",",
        f"    corp: \"{corp}\",",
        f"    category: {category},",
        f"    mass: {_ron_f32(mass)},",
        f"    hp: {hp},",
        f"    shape: Block(dims: ({dims[0]}, {dims[1]}, {dims[2]})),",
        "    mount_points: [",
    ]
    for mp in mount_points:
        lines.append(mount_point_to_ron(mp))
    lines.append("    ],")
    if components:
        lines.append("    components: [")
        for c in components:
            lines.append(f"        {c},")
        lines.append("    ],")
    else:
        lines.append("    components: [],")
    lines.append(f"    model_path: \"{model_path}\",")
    lines.append("    tags: [")
    for t in tags:
        lines.append(f"        \"{t}\",")
    lines.append("    ],")
    lines.append(")")
    return "\n".join(lines)


def module_id_from_name(name, corp):
    """对象名 → 游戏模块 id（id 前缀必须 == corp，nexus_core 校验）。

    2026-08-19 修复：对象名若已带 <corp>_ 前缀（如 'corp_tank'），先去掉——
    否则产出 'corp.corp_tank' 错误 id（命名规范见 docs/MODELING_EXPORT_GUIDE.md）。
    2026-08-21 P2-1：路径穿越防护——名字含斜杠/反斜杠/双点一律拒绝（防导出写出 out_dir）。
    """
    if any(seg in name for seg in ("/", "\\", "..")):
        raise ValueError(f"对象名含非法路径字符（斜杠/反斜杠/双点）：{name!r}")
    if name.startswith(f"{corp}_"):
        name = name[len(corp) + 1:]
    return f"{corp}.{name}"


def face_mark_to_mount_point(mark, dims):
    """面标记（局部面中心 + 法线）→ MountPoint 元组
    (cell_x, cell_y, cell_z, face, strength, accepts, layer, offset, align)。

    面中心 = 格中心 + 法线×0.5 → cell = round(中心 - 法线×0.5 - 0.5)。
    与 face_mark_from_geometry 的还原公式一致；越界返回 None（调用方过滤）。
    v31：offset 承载面内偏移（面调整 FaceAdjust 移动后的位置——旧实现
    恒 (0,0,0)，面内调整在导出时丢失；游戏端当前不消费 offset，但写正确
    为将来"连接点贴面任意位置"语义铺路）。
    """
    cx, cy, cz, nx, ny, nz = mark
    face = face_from_normal((nx, ny, nz))
    cell = (round(cx - nx * 0.5 - 0.5),
            round(cy - ny * 0.5 - 0.5),
            round(cz - nz * 0.5 - 0.5))
    if not (0 <= cell[0] < dims[0] and 0 <= cell[1] < dims[1] and 0 <= cell[2] < dims[2]):
        return None
    fc = face_mark_from_cell_face(cell, face)
    offset = (round(cx - fc[0], 6), round(cy - fc[1], 6), round(cz - fc[2], 6))
    return (cell[0], cell[1], cell[2], face, 100.0, "Any", 0, offset, False)


def mount_points_from_face_marks(marks, dims):
    """面标记列表 → 挂点列表（越界/非法自动过滤）。"""
    out = []
    for m in marks:
        mp = face_mark_to_mount_point(tuple(m), dims)
        if mp is not None:
            out.append(mp)
    return out


def mount_points_to_ron(mount_points):
    """挂点列表 → RON 文本（供导出拼装）。"""
    return "\n".join(mount_point_to_ron(mp) for mp in mount_points)


def validate_mount_points(mount_points, dims):
    """挂点合法性：cell 必须在 shape 内（nexus_core validate 同规则）。

    返回 (ok, errors)。
    """
    errors = []
    for (cx, cy, cz, face, _s, _a, _l, _o, _al) in mount_points:
        if not (0 <= cx < dims[0] and 0 <= cy < dims[1] and 0 <= cz < dims[2]):
            errors.append(f"cell ({cx},{cy},{cz}) 超出 shape {dims}")
        if face not in FACES:
            errors.append(f"face {face} 非法（须在 {FACES}）")
    # 连接点至少对齐网格（2026-08-19 用户定案："默认贴网格，连接点至少
    # 对齐网格"——cell 必须为整数格且已在 shape 内；导出链路 round 保证）
    return (len(errors) == 0, errors)


def validate_dims(dims):
    """尺寸规则（2026-08-19 用户："只要 Y Z X 任意一方不超过 2 和 2 就可以，
    一个再怎么长都可以，其他两个只能是两格"）——最多一轴 >2 格（不限长），
    其余两轴 ≤2。返回错误文案或 None。
    """
    long = sum(1 for d in dims if d > 2)
    if long > 1:
        return (f"尺寸超限：最多一轴可 >2 格（其余两轴 ≤2）——"
                f"当前 dims {tuple(dims)}，请拆分建模")
    return None


def validate_mesh_in_dims(obj, dims, tolerance=0.05):
    """网格永远限制模块（2026-08-19 用户："网格永远是限制模块的——建模
    不能大于网格，除非打特殊标签（动画/骨骼）才能打破"）。

    校验对象网格顶点（对齐后局部坐标）都在 (0..dims) 内；超出 tolerance
    即拒绝（建模大于网格）。返回错误文案或 None。
    """
    special = bool(obj.get("vf_special", 0))
    if special:
        return None  # 特殊标签（动画/骨骼）可打破
    try:
        inv = obj.matrix_world.inverted()
    except Exception:
        inv = __import__("mathutils").Matrix.Identity(4)  # 奇异矩阵回退
    for v in obj.data.vertices:
        p = inv @ obj.matrix_world @ v.co  # 世界坐标（对齐后=局部）
        if (p.x < -tolerance or p.y < -tolerance or p.z < -tolerance
                or p.x > dims[0] + tolerance or p.y > dims[1] + tolerance
                or p.z > dims[2] + tolerance):
            return (f"建模超出网格：顶点 ({p.x:.2f}, {p.y:.2f}, {p.z:.2f}) "
                    f"超出 dims {tuple(dims)}——网格永远限制模块（特殊标签除外）")
    return None


# ════════════════════════════════════════════════════════════════════
# 二、Blender 集成（仅在有 bpy 时注册——headless 单测不触发）
# ════════════════════════════════════════════════════════════════════

try:
    import bpy
    from bpy.types import Operator, Panel, PropertyGroup
    from bpy.props import (BoolProperty, EnumProperty, FloatProperty,
                           FloatVectorProperty, IntProperty, StringProperty)
    from mathutils import Vector
    HAS_BPY = True
except ImportError:
    HAS_BPY = False

# bl_info 必须在模块顶层——Blender 5.x 用 AST 预检 addon 的 bl_info，
# 只认顶层赋值；放在 if HAS_BPY: 块里会导致 "add-on missing bl_info"
# → 插件被拒绝加载（2026-08-21 根因实锤：面板/工具栏全部消失）
bl_info = {
    "name": "VoxelForge Connector",
    "author": "VoxelForge",
    "version": (3, 0, 0),
    # v31g：兼容 Blender 4.2——"blender" 字段是最低支持版本（4.2 会拒绝
    # 更高版本声明："was built for newer Blender"——用户环境 4.2/5.1/5.2 混装
    "blender": (4, 2, 0),
    "location": "3D View > N Panel > VoxelForge + 左侧工具栏'游戏'工具",
    "description": "面级连接点工具（点面=可连接）+ 一键导出 GLB/RON（1格=1米）",
    "category": "Object",
}

if HAS_BPY:

    # ════════════════════════════════════════════════════════════
    # "游戏"文字图标（Windows GDI 渲染 → 像素 → 三角形 → bpy.app.icons）
    # ════════════════════════════════════════════════════════════

    def _render_text_pixels(text, size=32, font_size=15):
        """Windows GDI 渲染中文字 → BGRA 像素 bytes（纯 CPU，任何模式可用）。

        DIB 32bpp 无 alpha 通道——用亮度判不透明（文字白色）。
        """
        import ctypes
        import ctypes.wintypes as wt
        import struct
        from ctypes import byref, create_string_buffer, c_void_p, string_at
        gdi32 = ctypes.WinDLL("gdi32")
        user32 = ctypes.WinDLL("user32")

        class LOGFONTW(ctypes.Structure):
            _fields_ = [
                ("lfHeight", ctypes.c_long), ("lfWidth", ctypes.c_long),
                ("lfEscapement", ctypes.c_long), ("lfOrientation", ctypes.c_long),
                ("lfWeight", ctypes.c_long), ("lfItalic", ctypes.c_byte),
                ("lfUnderline", ctypes.c_byte), ("lfStrikeOut", ctypes.c_byte),
                ("lfCharSet", ctypes.c_byte), ("lfOutPrecision", ctypes.c_byte),
                ("lfClipPrecision", ctypes.c_byte), ("lfQuality", ctypes.c_byte),
                ("lfPitchAndFamily", ctypes.c_byte),
                ("lfFaceName", ctypes.c_wchar * 32),
            ]

        hdc = gdi32.CreateCompatibleDC(None)
        lf = LOGFONTW(lfHeight=-font_size, lfWeight=400, lfCharSet=1,
                      lfQuality=4, lfFaceName="SimHei")
        hfont = gdi32.CreateFontIndirectW(byref(lf))
        gdi32.SelectObject(hdc, hfont)
        bmi = create_string_buffer(40)
        struct.pack_into("<i", bmi, 0, 40)
        struct.pack_into("<i", bmi, 4, size)
        struct.pack_into("<i", bmi, 8, -size)  # 负高 = 自上而下
        struct.pack_into("<h", bmi, 12, 1)
        struct.pack_into("<h", bmi, 14, 32)
        ppv = c_void_p()
        hbmp = gdi32.CreateDIBSection(hdc, bmi, 0, byref(ppv), None, 0)
        gdi32.SelectObject(hdc, hbmp)
        gdi32.SetBkMode(hdc, 1)  # TRANSPARENT
        gdi32.SetTextColor(hdc, 0x00FFFFFF)  # 白字
        rect = wt.RECT(0, 0, size, size)
        user32.DrawTextW(hdc, text, -1, byref(rect), 0x23)  # CENTER|VCENTER|SINGLELINE
        gdi32.GdiFlush()
        raw = string_at(ppv.value, size * size * 4)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteObject(hfont)
        gdi32.DeleteDC(hdc)
        return raw

    def _build_game_icon(size=32):
        """渲染'游戏'两字为工具栏图标（返回 icon_value；失败返回 0）。

        用户 2026-08-18："图标是'游戏'这两个字"——白字、透明底。
        纯 CPU（GDI）——background/GUI 模式均可用。

        Blender 5.2 的 bpy.app.icons.new_triangles(range, coords, colors) 是
        新格式（2026-08-19 用户截图：图标没出来——旧 floats 格式必抛
        ValueError，且 (0,1) range 只注册 1 个三角形）：
        - range: uchar 对（三角形索引范围，不参与 rasterize——绘制全部）
        - coords: bytes，每三角形 6 字节 = 3 顶点 × uchar XY（0-255 坐标空间）
        - colors: bytes，每三角形 12 字节 = 3 顶点 × RGBA（校验 2×coords）
        参考：BKE_icon_geom_rasterize 遍历全部 coords_len 三角形、按 256 光栅缩放。
        """
        try:
            raw = _render_text_pixels("游戏", size)
            coords = bytearray()
            colors = bytearray()
            for row in range(size):
                for col in range(size):
                    idx = (row * size + col) * 4
                    # DIB 无 alpha：亮度判不透明（白字）
                    if raw[idx] < 40 and raw[idx + 1] < 40 and raw[idx + 2] < 40:
                        continue
                    # uchar 坐标：光栅 y 0=顶部，DIB row 0=顶部——无需翻转
                    x0 = col * 255 // size
                    y0 = row * 255 // size
                    x1 = (col + 1) * 255 // size
                    y1 = (row + 1) * 255 // size
                    for (ax, ay, bx, by, cx, cy) in [
                        (x0, y0, x1, y0, x0, y1),
                        (x1, y1, x0, y1, x1, y0),
                    ]:
                        coords += bytes((ax, ay, bx, by, cx, cy))
                        colors += bytes((255, 255, 255, 255)) * 3
            if not coords:
                return 0
            return bpy.app.icons.new_triangles((0, 0), bytes(coords), bytes(colors))
        except Exception as e:
            print(f"[voxelforge_connector] 图标构建失败: {e}")
            return 0

    # ════════════════════════════════════════════════════════════
    # 游戏同款网格 + 连接面标记高亮（view3d draw handler）
    # ════════════════════════════════════════════════════════════

    _VF_DRAW_HANDLER = None

    _VF_DRAW_COUNT = {"n": 0}

    # 时机反馈（v31e 用户："会闪一下红色，各种东西你自己播动画"）：
    # 拒绝/异常 → 在拒绝点闪红（淡出）；成功/提示 → 文本留痕。draw 渲染。
    _VF_FEEDBACK = {"flash": None, "text": "", "text_until": 0.0}

    def _flash_feedback(world_pos, text, color=(1.0, 0.15, 0.15), duration=0.8,
                        size=2.0):
        """在世界位置播放瞬态反馈（默认闪红 0.8s 淡出 + 文本提示）。

        用户规则："没有贴近网格就不用受连接点管理（不加）+ 给提示"；
        "闪一下红色，各种东西你自己播动画"。到期后再重绘一次（清除残留）。
        """
        import time as _t
        _VF_FEEDBACK["flash"] = (list(world_pos), color, _t.time(),
                                 duration, float(size))
        _VF_FEEDBACK["text"] = text
        _VF_FEEDBACK["text_until"] = _t.time() + duration
        _tag_view_redraw()
        try:
            bpy.app.timers.register(_flash_expire_redraw,
                                    first_interval=duration + 0.15)
        except Exception:
            pass

    def _flash_expire_redraw():
        _tag_view_redraw()
        return None  # 一次性 timer

    def _tool_active_now(ttl=2.5):
        """连接点工具是否激活（v31f：draw_cursor 回调时间戳 < ttl 秒内）。
        工具激活时只画悬停单格网格（其他面不显示），未激活照旧全局网格。"""
        import time as _t
        return _t.time() - _VF_TOOL_ACTIVE["t"] < ttl

    @staticmethod  # 或 module 级（headless 可测）：反馈当前是否在显示
    def _flash_active():
        import time as _t
        return _VF_FEEDBACK["flash"] is not None and \
            _t.time() - _VF_FEEDBACK["flash"][2] < _VF_FEEDBACK["flash"][3]

    # 占用格缓存（按 mesh 顶点/面数——mesh 编辑后自动失效；避免每帧 ray_cast）
    _GRID_CACHE = {}
    # 空气连通集缓存（与 _GRID_CACHE 同 key——v31 空腔壁精确判定用）
    _AIR_CACHE = {}
    # 光标/工具状态（v31f：draw_cursor 回调带鼠标 xy——连接点悬停预览用）
    _VF_CURSOR = {"xy": None, "t": 0.0}
    _VF_TOOL_ACTIVE = {"t": 0.0}
    _VF_HOVER = {"cell": None, "face": None, "ok": False}

    def _grid_cache_key(obj):
        """占用格缓存 key（v31 加顶点采样指纹）。

        旧 key = (name, 顶点数, 面数, loc/scale 量化)——**顶点平移/编辑
        不改 counts 时缓存命中脏数据**（对齐/拖顶点后格框绿面错位）。
        采样指纹：均匀采样 ≤64 顶点 + 末顶点坐标加权和（1mm 量化），
        顶点位置变化 → 指纹变 → 缓存失效；成本 O(65)/调用。
        """
        q = lambda v, e=1e-3: round(v / e)  # 量化 1mm
        vs = obj.data.vertices
        n = len(vs)
        probe = 0.0
        if n:
            step = max(n // 64, 1)
            for i in range(0, n, step):
                c = vs[i].co
                probe += c.x * 3.1 + c.y * 1.7 + c.z * 2.3
            if n > step:
                c = vs[n - 1].co
                probe += c.x * 0.9 + c.y * 0.5 + c.z * 1.3
        return (obj.name, n, len(obj.data.polygons),
                q(obj.location[0]), q(obj.location[1]), q(obj.location[2]),
                q(obj.scale[0], 1e-3), q(obj.scale[1], 1e-3),
                q(obj.scale[2], 1e-3), round(probe * 1000))

    def _invalidate_grid_cache(obj):
        """显式清除某对象的占用格/空气缓存（顶点/变换被代码修改后调用——
        对齐/缩放等操作后 key 可能不变（采样指纹只在 _occupied_cells 调用时
        刷新），立即失效避免脏值。"""
        name = obj.name
        for cache in (_GRID_CACHE, _AIR_CACHE):
            for k in [k for k in cache if k[0] == name]:
                del cache[k]

    def _air_for(obj, cells, lb):
        """对象 + 占用格 → 外部连通空气集（缓存；cells 为回退值时不算）。"""
        key = _grid_cache_key(obj)
        if key in _AIR_CACHE:
            return _AIR_CACHE[key]
        air = external_air_cells(cells, lb)
        if key in _GRID_CACHE:  # 真值才缓存（回退全占用不污染）
            _AIR_CACHE[key] = air
        return air

    def _tag_view_redraw():
        """标记/状态写入后强制 3D 视口重绘（v31d：Blender 不会因 ID prop
        变化自动重绘——旧行为点击后绿面要等下次鼠标事件才出现 = 用户
        "点击过了很久才出现"根因）。headless/无窗口时静默跳过。"""
        try:
            for area in bpy.context.window_manager.windows[0].screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
        except Exception:
            pass

    def _occupied_cells(obj):
        """物体占用格列表 [(cx, cy, cz, occupied)]——含空心检测。

        用户 2026-08-19："空心建模按格数，超过格子就在那增加一个格；
        空心就可以在那里放其他模块"——格 AABB 与 mesh 相交测试
        （BVHTree.overlap）：有相交面=占用（白框），无=空心（青框，可放模块）。
        偶奇法则弃用：相邻格重叠面使射线步进误判（2026-08-19 实测）。
        缓存 key = 对象名 + 顶点/面数 + 位置/缩放量化 + **顶点采样指纹**
        （2026-08-22 性能：旧实现在 depsgraph 更新时全清缓存 → 点面等
        操作每次全量重算 BVH 卡顿；现缓存**自愈**——key 未变命中，网格/
        变换变化才失效。v31：加采样指纹——顶点平移/编辑不改 counts 时
        旧 key 不会失效，会导致对齐/拖顶点后格框错位）。
        大体积跳过检测。
        """
        import math
        import mathutils
        key = _grid_cache_key(obj)
        cached = _GRID_CACHE.get(key)
        if cached is not None:
            return cached
        b = _local_bounds(obj)
        mx = math.floor(b[0])
        my = math.floor(b[1])
        mz = math.floor(b[2])
        dx = max(int(math.ceil(b[3] - mx - 1e-6)), 1)
        dy = max(int(math.ceil(b[4] - my - 1e-6)), 1)
        dz = max(int(math.ceil(b[5] - mz - 1e-6)), 1)
        cells = []
        if dx * dy * dz <= 8192:
            try:
                from mathutils.bvhtree import BVHTree
                deps = bpy.context.evaluated_depsgraph_get()
                try:
                    deps.update()  # 确保 BVHTree 用最新 mesh（顶点编辑/对齐后）
                except Exception:
                    # draw 回调期间 depsgraph 禁止更新——用当前帧 depsgraph
                    # 构建 BVH（v31：不再整个回退全占用——否则首次 draw 绿面/
                    # 格框永远错，直到其它 operator 写入缓存）
                    pass
                bvh = BVHTree.FromObject(obj, deps)
                # 格=缩放空间；bvh=建模空间（bound_box 不含 scale）——
                # 检测坐标 ÷scale 换算（2026-08-21 修复）
                inv_sc = [1.0 / sc if abs(sc) > 1e-9 else 1e9
                          for sc in (obj.scale[0], obj.scale[1], obj.scale[2])]
                # 格子 AABB 内缩 0.01——共面接触不算相交（中心格贴着相邻
                # cube 但实际空心；2026-08-19 实测 overlap 共面误判占用）
                eps = 0.01
                axes = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
                # v31d 性能：全部格子三角形**一次构建一个 BVH + 单次 overlap**
                # ——旧实现每格 FromPolygons+overlap（20³=8000 次构建 ≈ 3.5s，
                # 大模型点击/编辑后"过长才出现"）。overlap 返回所有相交三角
                # 对（与逐格结果等价——同一算法）；格索引由 cell_of_face 回填。
                grid_cells = [(cx, cy, cz)
                              for cx in range(mx, mx + dx)
                              for cy in range(my, my + dy)
                              for cz in range(mz, mz + dz)]
                n_cells = len(grid_cells)
                all_verts = []
                all_faces = []
                cell_of_face = []  # 每格 12 三角 → 格索引
                for ci, (cx, cy, cz) in enumerate(grid_cells):
                    x0, x1 = cx + eps, cx + 1.0 - eps
                    y0, y1 = cy + eps, cy + 1.0 - eps
                    z0, z1 = cz + eps, cz + 1.0 - eps
                    corners = [
                        ((x0 * inv_sc[0], y0 * inv_sc[1], z0 * inv_sc[2])),
                        ((x1 * inv_sc[0], y0 * inv_sc[1], z0 * inv_sc[2])),
                        ((x1 * inv_sc[0], y1 * inv_sc[1], z0 * inv_sc[2])),
                        ((x0 * inv_sc[0], y1 * inv_sc[1], z0 * inv_sc[2])),
                        ((x0 * inv_sc[0], y0 * inv_sc[1], z1 * inv_sc[2])),
                        ((x1 * inv_sc[0], y0 * inv_sc[1], z1 * inv_sc[2])),
                        ((x1 * inv_sc[0], y1 * inv_sc[1], z1 * inv_sc[2])),
                        ((x0 * inv_sc[0], y1 * inv_sc[1], z1 * inv_sc[2])),
                    ]
                    base = len(all_verts)
                    all_verts.extend(corners)
                    for f in [(0, 1, 2), (0, 2, 3), (4, 5, 6), (4, 6, 7),
                              (0, 1, 5), (0, 5, 4), (2, 3, 7), (2, 7, 6),
                              (0, 3, 7), (0, 7, 4), (1, 2, 6), (1, 6, 5)]:
                        all_faces.append((f[0] + base, f[1] + base, f[2] + base))
                        cell_of_face.append(ci)
                # 占用位：0=未定 1=表面占用 2=空气 3=内部（回填）
                occ_state = [0] * n_cells
                try:
                    grid_bvh = BVHTree.FromPolygons(all_verts, all_faces)
                    for _ti, gi in bvh.overlap(grid_bvh):
                        ci = cell_of_face[gi]
                        if occ_state[ci] == 0:
                            occ_state[ci] = 1  # 表面格：相交 = 占用（快路径）
                except Exception:
                    # 大规模 FromPolygons 失败（极端）→ 回退逐格（慢但正确）
                    for ci, (cx, cy, cz) in enumerate(grid_cells):
                        x0, x1 = cx + eps, cx + 1.0 - eps
                        y0, y1 = cy + eps, cy + 1.0 - eps
                        z0, z1 = cz + eps, cz + 1.0 - eps
                        vecs = [mathutils.Vector(
                            (c[0] * inv_sc[0], c[1] * inv_sc[1], c[2] * inv_sc[2]))
                            for c in [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0),
                                      (x0, y1, z0), (x0, y0, z1), (x1, y0, z1),
                                      (x1, y1, z1), (x0, y1, z1)]]
                        cell_bvh = BVHTree.FromPolygons(vecs, [
                            (0, 1, 2), (0, 2, 3), (4, 5, 6), (4, 6, 7),
                            (0, 1, 5), (0, 5, 4), (2, 3, 7), (2, 7, 6),
                            (0, 3, 7), (0, 7, 4), (1, 2, 6), (1, 6, 5)])
                        if len(bvh.overlap(cell_bvh)) > 0:
                            occ_state[ci] = 1
                for ci, (cx, cy, cz) in enumerate(grid_cells):
                    if occ_state[ci] == 1:
                        # 表面格：面相交 = 占用，跳过偶奇（快）
                        cells.append((cx, cy, cz, True))
                        continue
                    # 非表面格：三轴偶奇多数表决（实心内部 vs 空心）
                    center_local = mathutils.Vector((
                        (cx + 0.5) * inv_sc[0],
                        (cy + 0.5) * inv_sc[1],
                        (cz + 0.5) * inv_sc[2]))
                    votes = 0
                    for ax in axes:
                        hits = 0
                        loc, _n, _i, _d = bvh.ray_cast(center_local, ax)
                        while loc is not None and hits < 64:
                            hits += 1
                            loc, _n, _i, _d = bvh.ray_cast(
                                loc + mathutils.Vector(
                                    (ax[0] * 0.001, ax[1] * 0.001, ax[2] * 0.001)),
                                ax)
                        if hits % 2 == 1:
                            votes += 1
                    inside = votes >= 2
                    cells.append((cx, cy, cz, inside))
            except Exception:
                # draw 回调里 deps.update 不允许（绘制期间）→ 回退全占用，
                # 不写缓存；正确值由 depsgraph_update_post handler 预热写入
                pass
        if not cells:
            # 异常/超限回退：全占用。不写缓存——draw 回调里 deps.update()
            # 会抛异常（绘制期间禁止更新 depsgraph），若缓存了回退值会一直
            # 命中脏数据；正确值由 depsgraph_update_post handler 预热写入
            cells = [(cx, cy, cz, True)
                     for cx in range(mx, mx + dx)
                     for cy in range(my, my + dy)
                     for cz in range(mz, mz + dz)]
        else:
            _GRID_CACHE[key] = cells
        return cells

    def _cell_edges(cx, cy, cz, out, mw=None):
        """单个 1×1×1 格框的 12 条边（追加到 out 顶点列表）。

        mw：对象矩阵（可选）——格线是局部坐标，渲染需转世界
        （2026-08-21 修复：模型带变换时网格与连接点错位）。
        """
        x0, y0, z0 = float(cx), float(cy), float(cz)
        x1, y1, z1 = x0 + 1.0, y0 + 1.0, z0 + 1.0

        def _t(x, y, z):
            if mw is None:
                return [x, y, z]
            v = mw @ Vector((x, y, z))
            return [v.x, v.y, v.z]

        out.append(_t(x0, y0, z0)); out.append(_t(x1, y0, z0))
        out.append(_t(x1, y0, z0)); out.append(_t(x1, y1, z0))
        out.append(_t(x1, y1, z0)); out.append(_t(x0, y1, z0))
        out.append(_t(x0, y1, z0)); out.append(_t(x0, y0, z0))
        out.append(_t(x0, y0, z1)); out.append(_t(x1, y0, z1))
        out.append(_t(x1, y0, z1)); out.append(_t(x1, y1, z1))
        out.append(_t(x1, y1, z1)); out.append(_t(x0, y1, z1))
        out.append(_t(x0, y1, z1)); out.append(_t(x0, y0, z1))
        out.append(_t(x0, y0, z0)); out.append(_t(x0, y0, z1))
        out.append(_t(x1, y0, z0)); out.append(_t(x1, y0, z1))
        out.append(_t(x1, y1, z0)); out.append(_t(x1, y1, z1))
        out.append(_t(x0, y1, z0)); out.append(_t(x0, y1, z1))

    def _grid_to_world_matrix(obj):
        """格空间→世界矩阵（2026-08-23 用户定案"一格=1 米固定"）。

        占用格坐标由 _occupied_cells 基于 _local_bounds 生成（格值=米，
        与世界同量纲——模型对齐后 location 整数格）。格坐标→世界：
        **只有平移**（location）——格值本身是世界米，无 scale 放大
        （1.3 米模型=2 格：格 0 (0..1) 格 1 (1..2)，格 1 中心=1.5 世界——
        按网格走，绿面在格上；scale=2 模型格 0..2 ✓）。

        旧实现 matrix_world×(1/scale) 在 scale≠1 时把格当成建模空间
        量纲，1.3 米模型格 1.5 直接乘错位（用户"体积出来了网格没跟上"）。
        """
        import mathutils
        return mathutils.Matrix.Translation(obj.location)

    def _mark_from_local(obj, center_local, normal_local):
        """建模空间局部面中心+法线 → 连接面标记（v31：位置×scale 转格空间）。

        标记坐标系 = **格空间**（世界米 - location，对齐后；格 = 局部×scale，
        与 _occupied_cells/_grid_to_world_matrix 同系）。局部坐标是建模空间
        （×scale=世界）——旧实现直接用局部坐标 → scale≠1 时标记错位：
        绿面画在模型内部/导出错格/点面被误拒"内面"（P1 实锤）。
        法线统一归一化——inv.to_3x3()@n 在 scale≠1 时非单位（scale=2 →
        (0,0.5,0)），face_mark_from_geometry 的位置公式按单位法向计算。
        """
        import math
        sc = [abs(s) if abs(s) > 1e-9 else 1e-9 for s in obj.scale]
        nl = math.sqrt(normal_local[0] ** 2 + normal_local[1] ** 2
                       + normal_local[2] ** 2)
        if nl < 1e-9:
            return None
        n = (normal_local[0] / nl, normal_local[1] / nl, normal_local[2] / nl)
        return face_mark_from_geometry(
            (center_local[0] * sc[0], center_local[1] * sc[1],
             center_local[2] * sc[2]), n)

    def _fill_vbo(vbo, pts):
        """Blender 5.2 attr_fill 填充（2026-08-22 绿面消失根因修复）：
        - pts 是 [x,y,z,...] 平铺 → 切成 [[x,y,z],...] 填充
        - pts 是 [[x,y,z],...] 嵌套 → 直接填充
        GPUVertBuf len = 顶点数（pts 长度即顶点数）。"""
        if pts and not isinstance(pts[0], (list, tuple)):
            n = len(pts) // 3
            data = [pts[i * 3:(i + 1) * 3] for i in range(n)]
        else:
            data = pts
        return vbo.attr_fill(0, data)

    def _vbo_len(pts):
        """GPUVertBuf 顶点数：nested=len，flat=len//3。"""
        if pts and not isinstance(pts[0], (list, tuple)):
            return len(pts) // 3
        return len(pts)

    def _vf_draw_cb():
        """POST_VIEW 绘制：游戏同款网格（可调）+ 已标记连接面高亮。

        用户 2026-08-18："网格必须给我显示，这个网格不是 blender 自己的
        网格，是我游戏的那个网格"——间距 1 米、范围 ±12 格、y=0 平面。
        用户 2026-08-19："网格虽然是自动的但是还是可以调整的"——
        面板新增：显示开关 / 范围（格数）/ 间距（米），实时生效。
        """
        import gpu
        from gpu.shader import from_builtin
        from gpu.types import GPUBatch, GPUVertBuf, GPUVertFormat
        import mathutils
        import math

        _VF_DRAW_COUNT["n"] += 1
        try:
            scene = bpy.context.scene
        except Exception as e:
            print(f"[voxelforge_connector] draw_cb: bpy.context 不可用: {e}")
            return
        grid_show = getattr(scene, "vf_grid_show", True)

        shader = from_builtin("UNIFORM_COLOR")
        fmt = GPUVertFormat()
        fmt.attr_add(id="pos", comp_type="F32", len=3, fetch_mode="FLOAT")

        # 体积网格（用户 2026-08-19 定案："按格数；超过格子就在那增加
        # 一个；空心建模的空心处可以放其他模块"）：
        # - 选中 MESH → 该物体绘制；无选中 → 所有 MESH 并集
        # - 占用格：**整体外框**（按最大边缘，2026-08-22 用户："那个框框是
        #   按最大的边缘来算的；不要给我搞这么多条缝 或者线太卡了"——
        #   大模块逐格 12 条边/格会画出上千条线卡死，改一个外框 12 条线）
        # - 空心格（包围盒内无体积）：青色逐格框=可放模块（数量少，保留）
        # - 无 MESH → 无网格
        occ_lines = []
        hole_lines = []
        # v31f：连接点工具激活时**不画全局体积网格**（用户："悬停那个地方
        # 周围要显示这一面的网格，其他面不显示网格"）——只显示悬停单格
        tool_active = _tool_active_now()
        if grid_show and not tool_active:
            objs = []
            ao = bpy.context.active_object
            if ao is not None and ao.type == "MESH":
                objs = [ao]
            else:
                objs = [o for o in bpy.data.objects if o.type == "MESH"]
            for obj in objs:
                cells = _occupied_cells(obj)
                mw_full = _grid_to_world_matrix(obj)
                occ = [(c[0], c[1], c[2]) for c in cells if c[3]]
                if occ:
                    occ_lines.extend(occ_outline_edges(occ, mw_full))
                for (cx, cy, cz, occupied) in cells:
                    if not occupied:
                        _cell_edges(cx, cy, cz, hole_lines, mw_full)
        if occ_lines:
            vbo = GPUVertBuf(fmt, len=_vbo_len(occ_lines))
            _fill_vbo(vbo, occ_lines)
            batch = GPUBatch(type="LINES", buf=vbo)
            shader.bind()
            shader.uniform_float("color", (1.0, 1.0, 1.0, 0.8))
            batch.draw(shader)
        if hole_lines:
            vbo = GPUVertBuf(fmt, len=_vbo_len(hole_lines))
            _fill_vbo(vbo, hole_lines)
            batch = GPUBatch(type="LINES", buf=vbo)
            shader.bind()
            shader.uniform_float("color", (0.4, 0.9, 1.0, 0.9))
            batch.draw(shader)

        # 连接面标记：亮绿实心整面（用户 2026-08-22："连接点渲染改成一整面"
        # + "连接点才这么小"——相邻同向面合并渲染：2×1 顶面 = 一个 2×1 大面，
        # 不再逐格画 1×1 小面；L 形贪心分解成矩形，绝不盖空气格），只画外面
        tri_pts = []
        line_pts = []
        for obj in bpy.data.objects:
            if obj.type != "MESH" or not obj.get("vf_connect_points"):
                continue
            mw = obj.matrix_world
            mw3 = mw.to_3x3()
            mw_full = _grid_to_world_matrix(obj)
            cells = _occupied_cells(obj)
            marks = [tuple(m) for m in obj["vf_connect_points"]]
            # 连接点渲染（2026-08-23 用户最终定案：一格=1 米固定；
            # "最大也一米，只不过按网格数量增加一点"——每个占用格面
            # 一个 1×1 米连接点，分散显示，不合并成整面；放大按格数
            # 增加；缩小跟网格走（<1 米也显示 1 米格）；"连接点保持
            # 不动"= 绿面世界尺寸恒 1 米 × cs，不随模型 scale 变）
            cs = float(getattr(scene, "vf_connect_scale", 1.0) or 1.0)
            su = sv = 0.5 * cs
            lb = _local_bounds(obj)
            air = _air_for(obj, cells, lb)  # 外部连通空气集（缓存，v31）
            occ_s = occupied_set(cells)  # v31d：预构建——is_exposed_face 不再每标记重建
            for m in marks:
                cf = mark_to_cell_face(m)
                if not is_exposed_face(cf, cells, lb, air, occ_s):
                    continue  # 只画外面（含空腔壁排除）
                # 标记位置（格空间=世界米-location，v31：格中心→mark 实际
                # 位置——面调整 FaceAdjust 移动的标记渲染跟手，不再钉在格心）
                wl = mathutils.Vector((m[0], m[1], m[2]))
                n = mathutils.Vector((m[3], m[4], m[5]))
                if n.length_squared < 1e-9:
                    continue
                n.normalize()
                if abs(n.z) < 0.9:
                    u = mathutils.Vector((0.0, 0.0, 1.0)).cross(n).normalized()
                else:
                    u = mathutils.Vector((1.0, 0.0, 0.0)).cross(n).normalized()
                v = n.cross(u).normalized()
                # 四角（本地格坐标）→ 世界
                c0 = mw_full @ (wl - u * su - v * sv)
                c1 = mw_full @ (wl + u * su - v * sv)
                c2 = mw_full @ (wl + u * su + v * sv)
                c3 = mw_full @ (wl - u * su + v * sv)
                # 外移 0.05（世界单位）防与模型面 z-fight
                wn = (mw3 @ n).normalized()
                o = wn * 0.05
                c0 += o
                c1 += o
                c2 += o
                c3 += o
                # 实心面：两个三角形（绕序 = u×v ≈ n，正对相机可见）
                for tri in [(c0, c1, c2), (c0, c2, c3)]:
                    for p in tri:
                        tri_pts.append([p.x, p.y, p.z])
                # 边框线（醒目）
                for (a, b) in [(c0, c1), (c1, c2), (c2, c3), (c3, c0)]:
                    line_pts.append([a.x, a.y, a.z])
                    line_pts.append([b.x, b.y, b.z])
        if tri_pts:
            gpu.state.blend_set("ALPHA")
            try:
                vbo3 = GPUVertBuf(fmt, len=_vbo_len(tri_pts))
                _fill_vbo(vbo3, tri_pts)
                batch3 = GPUBatch(type="TRIS", buf=vbo3)
                shader.uniform_float("color", (0.25, 0.95, 0.35, 0.35))
                batch3.draw(shader)
            finally:
                gpu.state.blend_set("NONE")
        if line_pts:
            vbo2 = GPUVertBuf(fmt, len=_vbo_len(line_pts))
            _fill_vbo(vbo2, line_pts)
            batch2 = GPUBatch(type="LINES", buf=vbo2)
            shader.uniform_float("color", (0.25, 0.95, 0.35, 1.0))
            batch2.draw(shader)

        # 悬停单格网格（v31f 用户："悬停那个地方周围要显示这一面的网格，
        # 其他面不显示网格；这网格是单格子算的"）：连接点工具激活时，
        # 鼠标所在模型面 → 该格 1×1 米格框（可标=亮黄绿，不可=红）+ 淡色面
        try:
            import time as _t
            if tool_active and _VF_CURSOR["xy"] is not None:
                region = bpy.context.region
                rv3d = bpy.context.region_data
                if region is not None and rv3d is not None:
                    from bpy_extras.view3d_utils import (
                        region_2d_to_origin_3d, region_2d_to_vector_3d)
                    co = _VF_CURSOR["xy"]
                    if 0 <= co[0] < region.width and 0 <= co[1] < region.height:
                        # v31j 性能：鼠标位置未变 → 跳过 ray_cast（悬停结果缓存，
                        # 省大场景每帧 scene.ray_cast——用户"有些东西过于慢"）
                        if _VF_HOVER.get("xy") == co:
                            self_skip = True  # 位置未变：不重算（结果与上帧相同）
                        else:
                            self_skip = False
                        if not self_skip:
                            origin = region_2d_to_origin_3d(region, rv3d, co)
                            direction = region_2d_to_vector_3d(region, rv3d, co)
                            deps_h = bpy.context.evaluated_depsgraph_get()
                            hit, loc, nrm, _fi, hit_obj, _m = bpy.context.scene.ray_cast(
                                deps_h, origin, direction)
                            _VF_HOVER.update({"xy": co, "cell": None,
                                              "face": None, "ok": False,
                                              "obj": None})
                            if hit and hit_obj is not None and hit_obj.type == "MESH":
                                inv_h = hit_obj.matrix_world.inverted()
                                center_local = inv_h @ loc
                                normal_local = inv_h.to_3x3() @ nrm
                                mark_h = _mark_from_local(
                                    hit_obj,
                                    (center_local.x, center_local.y,
                                     center_local.z),
                                    (normal_local.x, normal_local.y,
                                     normal_local.z))
                                if mark_h is not None:
                                    cfh = mark_to_cell_face(mark_h)
                                    cells_h = _occupied_cells(hit_obj)
                                    lb_h = _local_bounds(hit_obj)
                                    air_h = _air_for(hit_obj, cells_h, lb_h)
                                    ok_h = is_exposed_face(
                                        cfh, cells_h, lb_h, air_h,
                                        occupied_set(cells_h))
                                    _VF_HOVER.update({
                                        "cell": (cfh[0], cfh[1], cfh[2]),
                                        "face": cfh[3], "ok": ok_h,
                                        "obj": hit_obj.name})
                        # 绘制（用缓存的 _VF_HOVER——位置未变时沿用上帧结果，
                        # 预览不闪断；v31j 性能：鼠标不动不重射线）
                        self_skip = False  # 与上面分开控制（下段独立）
                        if not self_skip:
                            if _VF_HOVER.get("cell") is not None:
                                cfh = _VF_HOVER["cell"]
                                ok_h = _VF_HOVER["ok"]
                                hit_obj = bpy.data.objects.get(_VF_HOVER.get("obj"))
                                if hit_obj is not None:
                                    mw_h = _grid_to_world_matrix(hit_obj)
                                    cells_h = _occupied_cells(hit_obj)
                                    # v31g：悬停网格 = 鼠标悬停格周围**±2 格范围**
                                    #（用户："鼠标悬停的范围之内就 5 米多"——该面
                                    # 内不超过约 5×5 米格线，单格 1×1 米为单位）
                                    occ_h = occupied_set(cells_h)
                                    cx0, cy0, cz0 = cfh
                                    fh = _VF_HOVER.get("face", "Top")
                                    R = 2  # ±2 格 → 悬停周围 5 米多
                                    hpts = []
                                    if fh in ("Top", "Bottom"):
                                        near = [(gx, gy, gz) for (gx, gy, gz) in occ_h
                                                if gy == cy0 and
                                                abs(gx - cx0) + abs(gz - cz0) <= R]
                                    elif fh in ("East", "West"):
                                        near = [(gx, gy, gz) for (gx, gy, gz) in occ_h
                                                if gx == cx0 and
                                                abs(gy - cy0) + abs(gz - cz0) <= R]
                                    else:
                                        near = [(gx, gy, gz) for (gx, gy, gz) in occ_h
                                                if gz == cz0 and
                                                abs(gx - cx0) + abs(gy - cy0) <= R]
                                for (gx, gy, gz) in near:
                                    _cell_edges(gx, gy, gz, hpts, mw_h)
                                hv = GPUVertBuf(fmt, len=_vbo_len(hpts))
                                _fill_vbo(hv, hpts)
                                hb = GPUBatch(type="LINES", buf=hv)
                                if ok_h:
                                    hcol = (1.0, 0.95, 0.45, 1.0)
                                else:
                                    hcol = (1.0, 0.25, 0.25, 1.0)
                                shader.uniform_float("color", hcol)
                                hb.draw(shader)
                                # 单格淡色面（提示"就是这个格子"）——法向由
                                # face 查表（缓存路径无 mark_h）
                                _FACE_N = {"Top": (0.0, 1.0, 0.0),
                                           "Bottom": (0.0, -1.0, 0.0),
                                           "North": (0.0, 0.0, -1.0),
                                           "South": (0.0, 0.0, 1.0),
                                           "East": (1.0, 0.0, 0.0),
                                           "West": (-1.0, 0.0, 0.0)}
                                wl_h = mathutils.Vector(
                                    (cfh[0] + 0.5, cfh[1] + 0.5, cfh[2] + 0.5))
                                nh = mathutils.Vector(
                                    _FACE_N.get(fh, (0.0, 1.0, 0.0)))
                                if nh.length_squared > 1e-9:
                                    nh.normalize()
                                    if abs(nh.z) < 0.9:
                                        uh = mathutils.Vector(
                                            (0.0, 0.0, 1.0)).cross(nh).normalized()
                                    else:
                                        uh = mathutils.Vector(
                                            (1.0, 0.0, 0.0)).cross(nh).normalized()
                                    vh = nh.cross(uh).normalized()
                                    hq = [mw_h @ (wl_h - uh * 0.5 - vh * 0.5),
                                          mw_h @ (wl_h + uh * 0.5 - vh * 0.5),
                                          mw_h @ (wl_h + uh * 0.5 + vh * 0.5),
                                          mw_h @ (wl_h - uh * 0.5 + vh * 0.5)]
                                    hq_pts = [[p.x, p.y, p.z] for p in hq]
                                    gpu.state.blend_set("ALPHA")
                                    try:
                                        hf = GPUVertBuf(fmt, len=6)
                                        _fill_vbo(hf, [hq_pts[0], hq_pts[1], hq_pts[2],
                                                       hq_pts[0], hq_pts[2], hq_pts[3]])
                                        hfb = GPUBatch(type="TRIS", buf=hf)
                                        if ok_h:
                                            shader.uniform_float(
                                                "color", (0.55, 0.95, 0.35, 0.14))
                                        else:
                                            shader.uniform_float(
                                                "color", (1.0, 0.2, 0.2, 0.12))
                                        hfb.draw(shader)
                                    finally:
                                        gpu.state.blend_set("NONE")
        except Exception as e:
            print(f"[voxelforge_connector] hover 绘制失败: {e}")

        # 瞬态反馈 flash（v31e 用户"闪一下红色"）：正对相机的 1×1 米红面
        # 淡出 + 红色边框——被拒位置立即可见（不等鼠标事件）
        try:
            import time as _t
            fl = _VF_FEEDBACK.get("flash")
            if fl is not None:
                pos, fcolor, t0, dur, fsz = fl
                age = _t.time() - t0
                if age < dur:
                    a = max(0.05, 1.0 - age / dur) * 0.55
                    fc = mathutils.Vector(pos)
                    # 相机位置（区域视图矩阵）
                    cam = None
                    try:
                        vmat = bpy.context.region_data.view_matrix
                        cam = vmat.inverted().translation
                    except Exception:
                        pass
                    if cam is not None:
                        n = (cam - fc).normalized()
                    else:
                        n = mathutils.Vector((0.0, 1.0, 0.0))
                    if abs(n.z) < 0.9:
                        u = mathutils.Vector((0.0, 0.0, 1.0)).cross(n).normalized()
                    else:
                        u = mathutils.Vector((1.0, 0.0, 0.0)).cross(n).normalized()
                    v = n.cross(u).normalized()
                    h = 0.5 * fsz
                    q0 = fc - u * h - v * h
                    q1 = fc + u * h - v * h
                    q2 = fc + u * h + v * h
                    q3 = fc - u * h + v * h
                    fpts = [[q0.x, q0.y, q0.z], [q1.x, q1.y, q1.z],
                            [q2.x, q2.y, q2.z], [q3.x, q3.y, q3.z]]
                    gpu.state.blend_set("ALPHA")
                    try:
                        v4 = GPUVertBuf(fmt, len=4)
                        _fill_vbo(v4, fpts)
                        b4 = GPUBatch(type="TRI_FAN", buf=v4)
                        shader.uniform_float("color", (fcolor[0], fcolor[1],
                                                       fcolor[2], a))
                        b4.draw(shader)
                    finally:
                        gpu.state.blend_set("NONE")
                    lpts = []
                    for (pa, pb) in [(q0, q1), (q1, q2), (q2, q3), (q3, q0)]:
                        lpts.append([pa.x, pa.y, pa.z])
                        lpts.append([pb.x, pb.y, pb.z])
                    v5 = GPUVertBuf(fmt, len=_vbo_len(lpts))
                    _fill_vbo(v5, lpts)
                    b5 = GPUBatch(type="LINES", buf=v5)
                    shader.uniform_float("color", (fcolor[0], fcolor[1],
                                                   fcolor[2], a + 0.25))
                    b5.draw(shader)
                else:
                    _VF_FEEDBACK["flash"] = None
        except Exception as e:
            print(f"[voxelforge_connector] flash 绘制失败: {e}")


    # 工具图标缓存（注册时构建一次）
    _GAME_ICON = {"value": 0}

    class VF_FaceConnectTool(bpy.types.WorkSpaceTool):
        """点面标记连接点（再点取消）——工具栏最下方'游戏'工具"""

        bl_idname = "voxelforge.face_connect"
        bl_label = "游戏"
        bl_icon = "vf_game"  # 自定义图标（register 时注入 _icon_cache）
        bl_description = ("点击模型面 = 标记可连接（再点取消）\n"
                          "面标记随模块导出为连接点")
        bl_space_type = "VIEW_3D"
        bl_context_mode = "OBJECT"
        # 注意：WorkSpaceTool 无 bl_options——Blender 5.2 setup(options=...) 只接受
        # KEYMAP_FALLBACK/USE_BRUSHES，传 {"REGISTER","UNDO"} 会直接 ValueError
        # （2026-08-19 用户截图报错根源）。撤销行为由 bl_operator 的 Operator 决定。
        bl_keymap = ((
            "voxelforge.face_connect_toggle",
            # 用 PRESS 而非 CLICK——2026-08-19 高压测试复现：快速连点（<0.5s）
            # 被 Blender 判为 DOUBLE_CLICK，CLICK keymap 不触发 → 用户'按很多次才生效'；
            # 且点面时鼠标微动也会吞 CLICK。PRESS 按下即触发，单击/连点/微动都不丢。
            {"type": "LEFTMOUSE", "value": "PRESS"},
            {"properties": []},
        ), (
            # v31f：Shift+点击 = 批量连接点（用户："我打算按它就会批量的搞连接点"）
            # 冲突排查：3D View/Generic keymap 的 Shift+LEFTMOUSE 绑定 = 0，
            # Shift+点击加选是 view3d.select 内部逻辑（keymap 层无修饰绑定）——
            # 工具激活时本条目优先，无冲突。
            "voxelforge.face_connect_toggle",
            {"type": "LEFTMOUSE", "value": "PRESS", "shift": True},
            {"properties": []},
        ),)
        bl_operator = "voxelforge.face_connect_toggle"

        @classmethod
        def draw_settings(cls, context, layout, tool):
            obj = context.active_object
            if obj is None:
                layout.label(text="选择一个模型")
                return
            marks = obj.get("vf_connect_points", [])
            batch = obj.get("vf_batch_expand", False)
            cs = getattr(context.scene, "vf_connect_scale", 1.0)
            layout.label(text=f"已标记 {len(marks)} 个连接点"
                              f"（{'批量已展开' if batch else '默认单点'}）"
                              f"｜大小 {cs:.2f} 米/格")
            # v31c：显示当前连接点大小——0.667 残留值=绿面 2/3 来源（用户 08-23 截图）
            # 2026-08-23 用户："点开这个（游戏工具）它就默认就是调用了
            # （连接点）；然后它会悬停一个 UI，这个 UI 是 懂批量处理用的"——
            # 默认=连接点（单个点，已默认调用）；UI 悬停区 = 批量处理。
            box = layout.box()
            box.label(text="批量处理（悬停展开）", icon="TOOL_SETTINGS")
            col = box.column(align=True)
            op = col.operator("voxelforge.connect_expand",
                              text="批量展开连接点（中心→四周动画）")
            col.prop(op, "interval")
            col.operator("voxelforge.face_connect_batch", text="批量点（鼠标扫过逐格，右键取消）")
            col.operator("voxelforge.gen_mp", text="自动生成连接点（主面每格 1 米）")
            col.operator("voxelforge.clear_face_marks", text="取消全部标记")
            layout.label(text="默认=单个点（点面=标记/再点=取消）；"
                              "批量展开=从中心向四周（仅一面）", icon="MOUSE_LMB")
            layout.label(text="点击=该面按格连接（每格 1×1 米）；再点同面=整面取消；"
                              "Shift=自动生成（主面全格）", icon="MOUSE_LMB")
            # v31f：Shift 批量提示（用户："我打算按它就会批量的搞连接点"）
            layout.label(text="悬停=该面 ±2 格网格预览（约 5 米范围）", icon="MOUSE_LMB")
            # v31e：最近反馈文本（被拒/未贴格/批量提示——"给提示"）
            try:
                import time as _t
                if _VF_FEEDBACK["text"] and _t.time() < _VF_FEEDBACK["text_until"]:
                    layout.label(text=_VF_FEEDBACK["text"], icon="ERROR",
                                 translate=False)
            except Exception:
                pass

        @classmethod
        def draw_cursor(cls, context, tool, *args):
            # v31g：兼容新旧签名——Blender 4.2 旧签名 (context, draw, x, y)；
            # 5.2 新签名 (context, tool, xy)。4.2 传错签名→xy 解析错=鼠标判定全废
            if not args:
                return
            if len(args) == 1:
                xy = args[0]
            else:
                xy = (args[0], args[1])
            try:
                if len(xy) < 2:
                    xy = (float(xy[0]), 0.0)
            except Exception:
                return
            # Blender 5.2 新签名：(context, tool, xy)——旧 (context, draw, x, y) 会
            # TypeError（2026-08-19 GUI 实地验证发现）。用 blf 画提示文字。
            # 2026-08-23 用户"点击这个会有绿色的"：工具激活时光标旁画绿色
            # 圆点（连接点反馈——默认悬停即见）。
            # v31f：记录鼠标位置+激活时间戳（draw 用——悬停单格网格预览）
            import time as _t
            _VF_CURSOR["xy"] = (float(xy[0]), float(xy[1]))
            _VF_CURSOR["t"] = _t.time()
            _VF_TOOL_ACTIVE["t"] = _t.time()
            try:
                import blf
                import gpu
                from gpu.shader import from_builtin
                from gpu.types import GPUBatch, GPUVertBuf, GPUVertFormat
                font_id = 0
                blf.size(font_id, 12)
                blf.position(font_id, xy[0] + 14, xy[1] + 12, 0)
                blf.color(font_id, 1.0, 1.0, 1.0, 1.0)
                blf.draw(font_id, "连接")
                # 绿色圆点（4 边近似圆）——工具激活即见
                # v31 修复：draw_cursor 回调里 GPUBatch 默认沿用 3D 视图投影，
                # 屏幕坐标直接进 batch = 画进 3D 空间（用户截图：一条斜向绿线
                # 横穿视口顶部 = 圆点被投影拉成线）。必须显式设置 2D 正交矩阵
                # （region 像素空间）画出后恢复。
                shader = from_builtin("UNIFORM_COLOR")
                fmt = GPUVertFormat()
                fmt.attr_add(id="pos", comp_type="F32", len=3, fetch_mode="FLOAT")
                cx, cy = xy[0] + 4.0, xy[1] + 20.0
                import math as _m
                pts = []
                for i in range(12):
                    a = i * _m.tau / 12.0
                    pts.append([cx + _m.cos(a) * 4.0, cy + _m.sin(a) * 4.0, 0.0])
                vbo = GPUVertBuf(fmt, len=len(pts))
                vbo.attr_fill(0, pts)
                batch = GPUBatch(type="TRI_FAN", buf=vbo)
                shader.bind()
                shader.uniform_float("color", (0.25, 0.95, 0.35, 1.0))
                try:
                    gpu.matrix.push_projection()
                    # v31b 修正：Blender 5.2 无 gpu.matrix.ortho_projection——
                    # 手工构建 2D 正交投影（region 像素 → NDC）
                    _mw = float(context.region.width)
                    _mh = float(context.region.height)
                    import mathutils as _mu
                    _pm = _mu.Matrix.Identity(4)
                    _pm[0][0] = 2.0 / _mw
                    _pm[1][1] = 2.0 / _mh
                    _pm[0][3] = -1.0
                    _pm[1][3] = -1.0
                    gpu.matrix.load_projection_matrix(_pm)
                    batch.draw(shader)
                finally:
                    gpu.matrix.pop_projection()
            except Exception as e:
                print(f"[voxelforge_connector] draw_cursor 绘制失败: {e}")

    class VF_OT_FaceConnectToggle(Operator):
        """左键点面：标记该面可连接（再点取消）——面级连接点操作"""

        bl_idname = "voxelforge.face_connect_toggle"
        bl_label = "标记连接面"
        bl_description = "点击模型面：标记可连接（再点取消）"
        bl_options = {"REGISTER", "UNDO"}

        def invoke(self, context, event):
            region = context.region
            # 5.2：region_2d_to_* 第二参数必须是 RegionView3D（context.region_data），
            # 传 SpaceView3D（context.space_data）会 AttributeError（2026-08-19 用户截图）
            rv3d = context.region_data
            obj = context.active_object
            if obj is None or obj.type != "MESH":
                self.report({"WARNING"}, "请先选中一个网格模型")
                return {"CANCELLED"}
            # 2026-08-22 防呆：点面前自动对齐（未对齐时点击面反推格会错位）
            if _auto_align_if_needed(obj):
                self.report({"INFO"}, "模型未对齐——已自动对齐到网格")
            if rv3d is None:
                self.report({"WARNING"}, "当前区域无 3D 视图数据")
                return {"CANCELLED"}
            from bpy_extras.view3d_utils import region_2d_to_origin_3d, region_2d_to_vector_3d
            co = (event.mouse_region_x, event.mouse_region_y)
            origin = region_2d_to_origin_3d(region, rv3d, co)
            direction = region_2d_to_vector_3d(region, rv3d, co)
            deps = context.evaluated_depsgraph_get()
            hit, _loc, _norm, _face_idx, hit_obj, _matrix = context.scene.ray_cast(
                deps, origin, direction)
            if not hit or hit_obj != obj:
                # 未点到当前选中模型：尝试点到别的网格（自动切换选中）
                if hit and hit_obj.type == "MESH":
                    bpy.context.view_layer.objects.active = hit_obj
                    hit_obj.select_set(True)
                    obj = hit_obj
                else:
                    # 点到空白/非网格：明确反馈，避免用户以为没生效（2026-08-19）
                    self.report({"INFO"}, "未点到模型——请点击模型表面（再点同一面取消）")
                    return {"CANCELLED"}
            # 命中点（用户点击的真实位置——v31j 修复：旧实现用 _face_center_world
            # （整个多边形几何中心）——大面跨多格时（2×2×2 顶面=一个大面 4 格）
            # 无论点哪都反推到面中心同一格 → "点其他格子关闭第一个格子"；
            # 悬停预览（用 loc）与点击（用面中心）不一致 = 用户"鼠标判定错误"
            if _face_idx < 0 or _face_idx >= len(obj.data.polygons):
                # ray_cast 索引针对评估网格（对象带修改器时与原始网格不一致）
                self.report({"WARNING"}, "面索引越界（对象带修改器？请应用修改器后重试）")
                return {"CANCELLED"}
            inv = obj.matrix_world.inverted()
            center_local = inv @ Vector(loc)  # 命中点（点击的真实位置）
            normal_local = inv.to_3x3() @ _norm
            # 吸附到网格格（用户 2026-08-19："连接始终是对着网格来搞的，
            # 不是对着模型"）：统一走 face_mark_from_geometry（公式单点维护）
            # v31：位置×scale 转格空间——scale≠1 时建模空间坐标会错位
            # （绿面画在模型内部/点放大模型被误拒"内面"——实锤修复）
            mark = _mark_from_local(
                obj, (center_local.x, center_local.y, center_local.z),
                (normal_local.x, normal_local.y, normal_local.z))
            if mark is None:
                self.report({"WARNING"}, "无法解析面法线——请重试")
                return {"CANCELLED"}
            # 只对外面有效（用户 2026-08-21 定案）：内面（埋藏面）拒绝标记
            cells = _occupied_cells(obj)
            lb = _local_bounds(obj)
            air = _air_for(obj, cells, lb)
            occ_s = occupied_set(cells)
            cf = mark_to_cell_face(mark)
            if not is_exposed_face(cf, cells, lb, air, occ_s):
                # v31e：区分"未贴格不管理"（用户规则："没有贴近网格，那就
                # 不用受连接点管理了，直接不用加了"）与内面/空腔壁，都闪红
                if (cf[0], cf[1], cf[2]) not in occ_s:
                    msg = ("未贴近网格——该处不受连接点管理，未添加连接点"
                           "（连接点按格管理）")
                else:
                    msg = (f"{cf[3]} 面是内面（埋藏/空腔壁）——只对外面有效，"
                           "未添加连接点")
                w_p = _grid_to_world_matrix(obj) @ mathutils.Vector(
                    (mark[0], mark[1], mark[2]))
                _flash_feedback((w_p.x, w_p.y, w_p.z), msg)
                self.report({"WARNING"}, msg)
                return {"CANCELLED"}
            marks = list(obj.get("vf_connect_points", []))
            # v31i（用户三连修正："一面全部格子都占了这个是错的"——点面=单格
            # toggle：点一格=标这一格（按格=每连接点 1×1 米），再点=取消；
            # 批量走"批量点"工具（鼠标扫过逐格，方向自动判定上下/左右/前后）
            # 切换：同格同面已存在 → 取消；否则 → 添加
            for i, m in enumerate(marks):
                if mark_to_cell_face(m) == cf:
                    del marks[i]
                    obj["vf_connect_points"] = marks
                    _tag_view_redraw()
                    self.report({"INFO"}, "已取消该格连接（再点恢复）")
                    return {"FINISHED"}
            marks.append(mark)
            obj["vf_connect_points"] = marks
            _tag_view_redraw()
            self.report({"INFO"}, f"已标记连接格（共 {len(marks)} 个）")
            return {"FINISHED"}

    # ── 批量点（2026-08-23 v31i 用户："批量工具必须是鼠标经过……当一个格
    # 再磨出一个格子的时候就决定了它是否是上下或者左右"）──
    class VF_OT_FaceConnectBatch(Operator):
        """批量连接点（鼠标扫过）：按住左键扫过模型面 → 鼠标经过的格子
        **实时逐格**标记（每格 1×1 米）；方向由相邻两格自动判定（左右/上下/
        前后，结束提示）；右键/Esc 取消本次（只回滚新增）。"""
        bl_idname = "voxelforge.face_connect_batch"
        bl_label = "批量点（鼠标扫过）"
        bl_description = ("按住左键扫过模型面=经过的格逐格标连接点"
                          "（方向自动：左右/上下/前后）；右键/Esc 取消本次")
        bl_options = {"REGISTER"}

        def invoke(self, context, event):
            obj = context.active_object
            if obj is None or obj.type != "MESH":
                self.report({"WARNING"}, "请先选中一个网格模型")
                return {"CANCELLED"}
            if _auto_align_if_needed(obj):
                self.report({"INFO"}, "模型未对齐——已自动对齐到网格")
            self.obj = obj
            self.marks_before = list(obj.get("vf_connect_points", []))
            self.marked = 0
            self.last_cf = None       # 上一个格（方向判定基准）
            self.first_axis = None    # 首段方向（两格决定：x=左右 y=上下 z=前后）
            self.axes = set()
            self.cells = _occupied_cells(obj)
            self.lb = _local_bounds(obj)
            self.air = _air_for(obj, self.cells, self.lb)
            self.occ_s = occupied_set(self.cells)
            self.seen = {mark_to_cell_face(m) for m in self.marks_before}
            context.window_manager.modal_handler_add(self)
            self.report({"INFO"},
                        "批量点：按住左键扫过模型面（经过的格逐格标；"
                        "右键/Esc 取消）")
            return {"RUNNING_MODAL"}

        def modal(self, context, event):
            if event.type in {"ESC", "RIGHTMOUSE"}:
                self._restore()
                self.report({"INFO"}, "取消批量（本次新增已回滚）")
                return {"FINISHED"}
            if event.type == "LEFTMOUSE" and event.value == "RELEASE":
                axis = ""
                if self.first_axis:
                    labels = {"x": "左右", "y": "上下", "z": "前后"}
                    axis = labels.get(self.first_axis, "")
                    if len(self.axes) > 1:
                        axis = "混合"  # 多轴扫过（非横竖）
                self.report({"INFO"},
                            f"批量完成：{self.marked} 格"
                            + (f"（方向 {axis}）" if axis else "（未经过有效格）"))
                return {"FINISHED"}
            if event.type == "MOUSEMOVE":
                self._sweep(context, event.mouse_region_x, event.mouse_region_y)
            return {"RUNNING_MODAL"}

        def _push_axis(self, cf):
            """方向判定（用户："当一个格再磨出一个格子的时候就决定了它
            是否是上下或者左右"）：相邻两格坐标差 → 轴（x=左右/y=上下/
            z=前后）；2+ 轴同时非零=斜向（diag）；同格回访不改方向。"""
            if self.last_cf is None:
                self.last_cf = cf
                return
            dx = cf[0] - self.last_cf[0]
            dy = cf[1] - self.last_cf[1]
            dz = cf[2] - self.last_cf[2]
            nz = []
            if dx:
                nz.append("x")
            if dy:
                nz.append("y")
            if dz:
                nz.append("z")
            if not nz:
                self.last_cf = cf  # 同格回访：更新基准不判向
                return
            if len(nz) > 1:
                self.axes.add("diag")  # 斜向扫掠（混合）
                self.last_cf = cf
                return
            ax = nz[0]
            self.axes.add(ax)
            if self.first_axis is None:
                self.first_axis = ax
            self.last_cf = cf

        def _sweep(self, context, mx, my):
            """扫过一格（MOUSEMOVE）：命中暴露面 → 新格即添标记（实时）。"""
            from bpy_extras.view3d_utils import (
                region_2d_to_origin_3d, region_2d_to_vector_3d)
            rv3d = context.region_data
            if rv3d is None:
                return
            deps = context.evaluated_depsgraph_get()
            origin = region_2d_to_origin_3d(context.region, rv3d, (mx, my))
            direction = region_2d_to_vector_3d(context.region, rv3d, (mx, my))
            hit, loc, _n, _fi, hit_obj, _m = context.scene.ray_cast(
                deps, origin, direction)
            if not hit or hit_obj != self.obj:
                self.last_cf = None  # 离开模型：清基准（下次进入重新定向）
                return
            inv = self.obj.matrix_world.inverted()
            center_local = inv @ loc
            normal_local = inv.to_3x3() @ _n
            mark = _mark_from_local(
                self.obj, (center_local.x, center_local.y, center_local.z),
                (normal_local.x, normal_local.y, normal_local.z))
            if mark is None:
                return
            cf = mark_to_cell_face(mark)
            if not is_exposed_face(cf, self.cells, self.lb, self.air,
                                   self.occ_s):
                return  # 未贴格/内面/空腔壁不加
            if cf in self.seen:
                self._push_axis(cf)
                return
            self._push_axis(cf)
            self.seen.add(cf)
            marks = list(self.obj.get("vf_connect_points", []))
            marks.append(mark)
            self.obj["vf_connect_points"] = marks
            self.marked += 1
            _tag_view_redraw()  # 扫过即见（实时）

        def _restore(self):
            """回滚本次批量（还原 invoke 前的标记列表）。"""
            self.obj["vf_connect_points"] = self.marks_before
            _tag_view_redraw()

    # ── AI 一键管道（2026-08-22 用户："插件对接AI 至少能让AI搞的舒服"）──
    # AI/脚本只需一个 operator：对齐 → 生成主连接面 → 校验 → 导出 RON+GLB，
    # 全自动无人工干预——headless+GUI 皆可调（blender --background --python
    # voxelforge_connector.py -- --auto-pipeline <dir> 等价）。
    class VF_OT_AutoPipeline(Operator):
        """AI 一键管道：对齐→主面→校验→导出（headless/GUI 皆可）。"""
        bl_idname = "voxelforge.auto_pipeline"
        bl_label = "AI 一键管道（对齐→主面→导出）"
        bl_description = ("对选中/全部网格物体：自动对齐→生成主连接面→校验→"
                          "导出 RON+GLB——AI/脚本单命令调用")

        out_dir: StringProperty(name="输出目录", default="", subtype="DIR_PATH")

        def execute(self, context):
            import tempfile
            out = self.out_dir or tempfile.mkdtemp(prefix="vf_out_")
            os.makedirs(out, exist_ok=True)
            objs = [o for o in bpy.data.objects if o.type == "MESH"]
            if not objs:
                self.report({"WARNING"}, "没有网格对象")
                return {"CANCELLED"}
            count, fail = 0, []
            for obj in objs:
                bpy.context.view_layer.objects.active = obj
                # 1) 自动对齐（防呆已含）
                _auto_align_if_needed(obj)
                # 2) 生成主连接面（一面）
                cells = _occupied_cells(obj)
                face, cells_on = primary_face_for_module(cells, _local_bounds(obj))
                if face is None:
                    fail.append(f"{obj.name}: 无暴露面")
                    continue
                marks = [face_mark_from_cell_face(g, face) for g in cells_on]  # 每格 1 点
                old = list(obj.get("vf_connect_points", []))
                obj["vf_connect_points"] = merge_face_marks(
                    [m for m in old if mark_to_cell_face(m)[3] != face] + marks)
                obj["vf_batch_expand"] = True
                _tag_view_redraw()
                # 3) 校验 + 导出
                dims = dims_from_bounds(_bounds_of(obj))
                mps = mount_points_from_face_marks(
                    list(obj.get("vf_connect_points", [])), dims)
                ok, errs = validate_mount_points(mps, dims)
                if not ok:
                    fail.append(f"{obj.name}: {'; '.join(errs)}")
                    continue
                module_id = module_id_from_name(obj.name, "corp")
                ron = export_module_ron(
                    module_id=module_id, name=obj.name, corp="corp",
                    category=category_from_name(obj.name),
                    mass=10.0, hp=100, dims=dims, mount_points=mps,
                    model_path=f"models/corp/{module_id}.glb", tags=[],
                )
                with open(os.path.join(out, f"{module_id}.ron"),
                          "w", encoding="utf-8") as f:
                    f.write(ron)
                count += 1
            self.report({"INFO"}, f"AI 管道完成：{count} 导出 / {len(fail)} 失败 → {out}")
            for e in fail:
                print(f"[voxelforge_connector] AI 管道失败: {e}")
            return {"FINISHED"}

    # ── 批量展开连接点（2026-08-23 用户："把它变大，批量处理条件下它
    # 就会增长……从中心点（最先没被处理的）启动，向四周展开，只限一面，
    # 动画比较慢……全部展开连接点多多耗性能，这也是这个动画意义"）──
    class VF_OT_ConnectExpand(Operator):
        """批量展开连接点（动画）：从主面中心格开始逐环向四周扩散，
        直到铺满主面（只限一面）。慢速渐进 = 不全量瞬间开（省性能）。
        左键点击开始；ESC/右键 = 暂停本次展开（保留已展开部分）。"""
        bl_idname = "voxelforge.connect_expand"
        bl_label = "批量展开连接点（中心→四周）"
        bl_description = ("从主面中心格向四周逐环展开连接点（限一面）；"
                          "慢速渐进；ESC/右键暂停")
        bl_options = {"REGISTER"}

        interval: FloatProperty(
            name="展开速度", default=0.22, min=0.05, max=2.0,
            description="每环间隔秒数（慢速渐进——用户'这个动画还是比较慢的'）")

        def invoke(self, context, event):
            obj = context.active_object
            if obj is None or obj.type != "MESH":
                self.report({"WARNING"}, "请先选中一个网格模型")
                return {"CANCELLED"}
            if _auto_align_if_needed(obj):
                self.report({"INFO"}, "模型未对齐——已自动对齐到网格")
            cells = _occupied_cells(obj)
            face, cells_on = primary_face_for_module(cells, _local_bounds(obj))
            if face is None:
                self.report({"WARNING"}, "没有暴露面")
                return {"CANCELLED"}
            # 未展开目标格（去重：不在已有标记中）
            existing = {mark_to_cell_face(m)[:3] for m in obj.get("vf_connect_points", [])}
            todo = [c for c in cells_on if c not in existing]
            if not todo:
                self.report({"INFO"}, "主面连接点已全部展开")
                return {"CANCELLED"}
            center = face_center_cell(face, cells_on)
            rings = face_expand_rings(face, todo, center)
            self.obj = obj
            self.face = face
            self.rings = rings
            self.ring_i = 0
            self.added = 0
            wm = context.window_manager
            wm.modal_handler_add(self)
            self._timer = wm.event_timer_add(self.interval, window=context.window)
            self.report({"INFO"},
                        f"批量展开：{face} 面中心 {center} → 四周 "
                        f"（{len(todo)} 格 / {len(rings)} 环，ESC 暂停）")
            return {"RUNNING_MODAL"}

        def modal(self, context, event):
            if event.type == "TIMER":
                if self.ring_i < len(self.rings):
                    ring = self.rings[self.ring_i]
                    marks = list(self.obj.get("vf_connect_points", []))
                    for g in ring:
                        marks.append(face_mark_from_cell_face(g, self.face))
                    self.obj["vf_connect_points"] = merge_face_marks(marks)
                    self.added += len(ring)
                    self.ring_i += 1
                    self.obj["vf_batch_expand"] = True
                    bpy.context.view_layer.update()
                    self.obj.update_tag()
                    _tag_view_redraw()
                    if self.ring_i >= len(self.rings):
                        context.window_manager.event_timer_remove(self._timer)
                        self.report({"INFO"},
                                    f"批量展开完成：{self.face} 面全部 "
                                    f"{len(self.obj.get('vf_connect_points', []))} 标记")
                        return {"FINISHED"}
                    return {"RUNNING_MODAL"}
                context.window_manager.event_timer_remove(self._timer)
                return {"FINISHED"}
            if event.type in {"ESC", "RIGHTMOUSE"} and event.value == "PRESS":
                context.window_manager.event_timer_remove(self._timer)
                self.report({"INFO"},
                            f"批量展开暂停：已展开 {self.ring_i}/{len(self.rings)} 环"
                            f"（{self.added} 格）——可再点继续")
                return {"FINISHED"}
            return {"RUNNING_MODAL"}

    class VF_OT_ClearFaceMarks(Operator):
        """清除选中对象的全部面标记"""

        bl_idname = "voxelforge.clear_face_marks"
        bl_label = "清除连接标记"
        bl_options = {"REGISTER", "UNDO"}

        def execute(self, context):
            obj = context.active_object
            if obj is not None and obj.get("vf_connect_points"):
                del obj["vf_connect_points"]
                # v31：清除标记同时重置批量展开状态（UI"批量已展开"残留）
                if obj.get("vf_batch_expand"):
                    del obj["vf_batch_expand"]
                _tag_view_redraw()
                self.report({"INFO"}, "已清除全部连接标记")
            return {"FINISHED"}

    # ── 工具函数 ──
    def _bounds_of(obj):
        """对象世界包围盒 (min_x, min_y, min_z, max_x, max_y, max_z)。"""
        corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
        min_c = [min(c[i] for c in corners) for i in range(3)]
        max_c = [max(c[i] for c in corners) for i in range(3)]
        return (min_c[0], min_c[1], min_c[2], max_c[0], max_c[1], max_c[2])

    def _local_bounds(obj):
        """对象缩放空间包围盒——格系统基准（2026-08-21 修复）。

        格 = 建模空间 × obj.scale（bound_box 是建模空间，不含 scale）：
        标记/格/校验/渲染全链路一致；模型不在原点/带缩放时不再错位
        （用户实锤："连接点渲染到其他地方了"）。
        """
        sc = [s if abs(s) > 1e-9 else 1e-9 for s in obj.scale]
        corners = [[c[i] * sc[i] for i in range(3)] for c in obj.bound_box]
        min_c = [min(c[i] for c in corners) for i in range(3)]
        max_c = [max(c[i] for c in corners) for i in range(3)]
        return (min_c[0], min_c[1], min_c[2], max_c[0], max_c[1], max_c[2])

    def _auto_align_if_needed(obj):
        """未对齐自动修复（2026-08-22 用户"绿面偏移"防呆）：
        模型不在格 0 角（bound_box 局部 min 非 0 或位置带小数偏移）时
        自动执行对齐——不等用户先点"对齐到网格"（跳过该步是常态，
        导致占格/标记/渲染错位）。"""
        bpy.context.view_layer.update()  # bound_box 是缓存——顶点/位置改动后必须刷新
        b = _bounds_of(obj)
        min_c = [b[0], b[1], b[2]]
        # 浮点容差判"整数格"：floor 差对 1.9999999 会判 1.0（假未对齐，
        # 2026-08-22 v26 实测）——用 round 差（精确到 1e-4）
        def _is_int(x):
            return abs(x - round(x)) < 1e-4
        # 位置已整数格且包围盒 min 与 location 一致 → 已对齐
        if all(_is_int(v) for v in min_c) and \
           all(abs(min_c[i] - obj.location[i]) < 1e-3 for i in range(3)):
            return False
        # 执行对齐（与 VF_OT_AlignGrid 同逻辑）
        inv = obj.matrix_world.inverted()
        local_min = inv @ Vector((b[0], b[1], b[2]))
        # v31d 性能修复：顶点平移用 numpy foreach（旧 Python 循环 O(V)——
        # 已应用细分的大网格几万顶点=秒级，用户"点击过了很久才出现"根因）
        try:
            import numpy as np
            n = len(obj.data.vertices)
            buf = np.zeros(n * 3, dtype=np.float32)
            obj.data.vertices.foreach_get("co", buf)
            buf -= np.array((local_min.x, local_min.y, local_min.z),
                            dtype=np.float32)
            obj.data.vertices.foreach_set("co", buf)
        except Exception:
            for v in obj.data.vertices:
                v.co -= local_min
        obj.location = Vector((int(b[0]), int(b[1]), int(b[2])))
        bpy.context.view_layer.update()
        _invalidate_grid_cache(obj)  # 顶点被改（v31：防 key 不变命中脏缓存）
        return True

    def _apply_translate(obj, delta):
        obj.location += Vector(delta)
        # 子级 MP_ 跟随（相对坐标不变——挂在对象下的 Empty 自动跟随）

    # ── 网格对齐 ──
    class VF_OT_AlignGrid(Operator):
        bl_idname = "voxelforge.align_grid"
        bl_label = "对齐到网格"
        bl_description = "包围盒最小角取整落格 + 对象原点移到格 0 角（左下角）"

        def execute(self, context):
            for obj in context.selected_objects:
                if obj.type != "MESH":
                    continue
                b = _bounds_of(obj)
                # 1) 顶点局部平移：对象空间最小角 → 局部原点（左下角 = 格 0 角）
                inv = obj.matrix_world.inverted()
                local_min = inv @ Vector((b[0], b[1], b[2]))
                # v31d：numpy foreach（大网格顶点循环→毫秒级）
                try:
                    import numpy as np
                    n = len(obj.data.vertices)
                    buf = np.zeros(n * 3, dtype=np.float32)
                    obj.data.vertices.foreach_get("co", buf)
                    buf -= np.array((local_min.x, local_min.y, local_min.z),
                                    dtype=np.float32)
                    obj.data.vertices.foreach_set("co", buf)
                except Exception:
                    for v in obj.data.vertices:
                        v.co -= local_min
                # 2) 对象位置 = 世界最小角（向下取整落整数格）
                obj.location = Vector((int(b[0]), int(b[1]), int(b[2])))
                _invalidate_grid_cache(obj)  # v31：顶点被改——缓存立即失效
            self.report({"INFO"}, "已对齐到网格（原点=格 0 角）")
            return {"FINISHED"}

    class VF_OT_AlignCenter(Operator):
        bl_idname = "voxelforge.align_center"
        bl_label = "对齐中心"
        bl_description = "包围盒中心平移到最近整数格（多格模块中心落格线）"

        def execute(self, context):
            for obj in context.selected_objects:
                if obj.type != "MESH":
                    continue
                _apply_translate(obj, align_center_offset(_bounds_of(obj)))
                _invalidate_grid_cache(obj)  # v31：位置变——缓存立即失效
            self.report({"INFO"}, "已对齐中心")
            return {"FINISHED"}

    class VF_OT_ScaleDims(Operator):
        bl_idname = "voxelforge.scale_dims"
        bl_label = "按格数缩放"
        bl_description = "缩放到 x×y×z 米（1×1 占位模型快速拉长）"

        scale_x: FloatProperty(name="X 格数", default=1.0, min=0.1)
        scale_y: FloatProperty(name="Y 格数", default=1.0, min=0.1)
        scale_z: FloatProperty(name="Z 格数", default=1.0, min=0.1)

        def execute(self, context):
            for obj in context.selected_objects:
                if obj.type != "MESH":
                    continue
                s = scale_to_dims(_bounds_of(obj),
                                  (self.scale_x, self.scale_y, self.scale_z))
                obj.scale = (obj.scale[0] * s[0], obj.scale[1] * s[1], obj.scale[2] * s[2])
                _invalidate_grid_cache(obj)  # v31：scale 变——缓存立即失效
            self.report({"INFO"}, "已按格数缩放")
            return {"FINISHED"}

    # ── 连接点 ──
    class VF_OT_GenMountPoints(Operator):
        """自动生成连接点（2026-08-23 v31d 用户定案："2×2 模型=4 个连接点，
        每格 1×1 米"——主面每格 1×1 米分散；"误判了一面也就是一米"=旧实现
        把 2 米（2 格）顶面只生成 1 个 1 米点）。

        游戏工具点面交互仍是单点（点一下=标 1 格，未点批量不涨——
        2026-08-23 早上定案）——那是交互工具；本按钮=按规则自动生成。
        """
        bl_idname = "voxelforge.gen_mp"
        bl_label = "自动生成连接点（主面每格 1 米）"
        bl_description = "主面每占用格生成 1×1 米连接点（2×2 模块=4 个；一格一米）"

        def execute(self, context):
            obj = context.active_object
            if obj is None or obj.type != "MESH":
                self.report({"ERROR"}, "请选中一个网格对象")
                return {"CANCELLED"}
            # 2026-08-22 防呆：模型未对齐（建模常态——直接点自动生成）时
            # 先自动对齐，占格/标记/渲染才与模型一致（用户"绿面偏移"根因）
            if _auto_align_if_needed(obj):
                self.report({"INFO"}, "模型未对齐——已自动对齐到网格")
            cells = _occupied_cells(obj)
            face, cells_on = primary_face_for_module(cells, _local_bounds(obj))
            if face is None:
                b = _bounds_of(obj)
                _flash_feedback(((b[0] + b[3]) * 0.5, (b[1] + b[4]) * 0.5,
                                 (b[2] + b[5]) * 0.5,),
                                "没有暴露面——模型全被占用格包围，无法生成连接点")
                self.report({"WARNING"}, "没有暴露面（模型全被占用格包围？）")
                return {"CANCELLED"}
            center = face_center_cell(face, cells_on)
            marks = [face_mark_from_cell_face(g, face) for g in cells_on]  # 每格 1 点
            old = list(obj.get("vf_connect_points", []))
            # v31d：自动生成 = 主面**每格 1×1 米分散**（用户定案"放大=按格数
            # 分散增加；2×2 模型=4 个连接点，每格 1×1 米"——旧"中心 1 点"
            # 把一面（2 米=4 格）当成 1 米 1 格 = 用户"误判了一面也就是一米"）。
            # 主面旧标记整体替换（含面内移动过的），其它面旧标记保留。
            merged = merge_face_marks(
                [m for m in old if mark_to_cell_face(m)[3] != face] + marks)
            obj["vf_connect_points"] = merged
            obj["vf_batch_expand"] = True  # 每格分散 = 已展开状态
            _tag_view_redraw()
            cs = getattr(context.scene, "vf_connect_scale", 1.0)
            self.report({"INFO"},
                        f"已生成 {len(merged)} 个连接点（{face} 面"
                        f" {len(cells_on)} 格，每格 1×1 米；"
                        f"当前大小 {cs:.2f} 米/格）")
            return {"FINISHED"}

    class VF_OT_MarkSelectedFaces(Operator):
        """Edit Mode 选中多个面 → 一键标记连接面（复杂模型批量标记）。

        2026-08-21 用户："复杂的多边形连接点"——逐面点选效率极低；
        选中面批量标记 + 同格同向去重合并（斜切/细分一格多面只留一个点）。
        """
        bl_idname = "voxelforge.mark_selected_faces"
        bl_label = "标记选中面为连接点"
        bl_description = "Edit Mode 选中多个面 → 批量标记连接点（同格同向自动合并）"
        bl_options = {"REGISTER", "UNDO"}

        def execute(self, context):
            obj = context.active_object
            if obj is None or obj.type != "MESH":
                self.report({"ERROR"}, "请选中一个网格对象")
                return {"CANCELLED"}
            if obj.mode != "EDIT":
                self.report({"WARNING"}, "请进入编辑模式（Tab）并选中面")
                return {"CANCELLED"}
            import bmesh
            bm = bmesh.from_edit_mesh(obj.data)
            selected = [f for f in bm.faces if f.select]
            if not selected:
                self.report({"WARNING"}, "未选中任何面（Edit Mode 选中面后重试）")
                return {"CANCELLED"}
            marks = list(obj.get("vf_connect_points", []))
            new_marks = []
            for f in selected:
                # v31：bmesh 的 center/normal 已是局部（建模空间）——旧实现
                # 再乘 inv 是双重转换（scale≠1 时位置错误折叠）；直接 ×scale
                # 转格空间（_mark_from_local 内完成）
                center = f.calc_center_median()
                normal = f.normal
                mk = _mark_from_local(
                    obj, (center.x, center.y, center.z),
                    (normal.x, normal.y, normal.z))
                if mk is not None:
                    new_marks.append(mk)
            merged = merge_face_marks(new_marks)
            before = len(marks)
            marks = merge_face_marks(marks + merged)
            obj["vf_connect_points"] = marks
            _tag_view_redraw()
            added = len(marks) - before
            self.report({"INFO"},
                        f"已标记 {len(merged)} 个连接面（选中 {len(selected)} 面，"
                        f"合并去重后新增 {added}；共 {len(marks)} 个）")
            return {"FINISHED"}

    class VF_OT_ValidateMPs(Operator):
        """校验连接点 vs 占用格：浮空/埋内部/冗余三类问题清单。

        2026-08-21 重构：面标记为唯一数据源。
        """
        bl_idname = "voxelforge.validate_mp"
        bl_label = "校验连接点"
        bl_description = "检查浮空（空气格）/埋内部（被挡）/冗余（重复）三类问题"
        bl_options = {"REGISTER", "UNDO"}

        def execute(self, context):
            obj = context.active_object
            if obj is None or obj.type != "MESH":
                self.report({"ERROR"}, "请选中一个网格对象")
                return {"CANCELLED"}
            cells = _occupied_cells(obj)
            marks = list(obj.get("vf_connect_points", []))
            problems = []
            for m in marks:
                for kind, msg in validate_mp_against_cells([mark_to_cell_face(m)], cells):
                    problems.append((kind, msg))
            if not problems:
                self.report({"INFO"}, f"连接点全部合法（{len(marks)} 个标记）")
                return {"FINISHED"}
            kinds = [k for k, _ in problems]
            self.report({"WARNING"},
                        f"发现 {len(problems)} 个问题：浮空 {kinds.count('float')}、"
                        f"埋内部 {kinds.count('buried')}、冗余 {kinds.count('duplicate')}——"
                        f"详见控制台")
            for kind, msg in problems:
                print(f"[voxelforge_connector] 校验 {kind}: {msg}")
            return {"FINISHED"}

    class VF_OT_FixMPs(Operator):
        """一键修复（2026-08-21 重构）：删除浮空/埋内部/冗余的连接点标记。"""
        bl_idname = "voxelforge.fix_mp"
        bl_label = "一键修复连接点"
        bl_description = "自动删除浮空/埋内部/冗余的连接点标记"
        bl_options = {"REGISTER", "UNDO"}

        def execute(self, context):
            obj = context.active_object
            if obj is None or obj.type != "MESH":
                self.report({"ERROR"}, "请选中一个网格对象")
                return {"CANCELLED"}
            cells = _occupied_cells(obj)
            marks = list(obj.get("vf_connect_points", []))
            if not marks:
                self.report({"INFO"}, "没有连接点标记")
                return {"FINISHED"}
            good = []
            removed = 0
            for m in marks:
                errs = validate_mp_against_cells([mark_to_cell_face(m)], cells)
                if errs:
                    removed += 1
                else:
                    good.append(m)
            obj["vf_connect_points"] = good
            _tag_view_redraw()
            if removed == 0:
                self.report({"INFO"}, "没有需要修复的连接点（全部合法）")
            else:
                self.report({"INFO"}, f"已修复：删除 {removed} 个问题标记"
                                      f"（剩余 {len(good)} 个）")
            return {"FINISHED"}

    class VF_OT_FaceAdjust(Operator):
        """面调整（2026-08-21 用户定案：编辑=面调整——标记版）。

        点选一个连接面标记 → 拖动只在**所在格子的那一面**内左右上下移动：
        - 不超格子/不低于格子（clamp_mark_to_face 钳制，基准=标记原 cell）
        - 法向固定（不会离开这一面）；作用只在这一面（只改选中的标记）
        """
        bl_idname = "voxelforge.face_adjust"
        bl_label = "面内调整连接点"
        bl_description = "点选连接点标记后拖动：只在所在格子的面内左右上下调整"
        bl_options = {"REGISTER", "UNDO"}

        @classmethod
        def poll(cls, context):
            return context.active_object is not None and                 context.active_object.type == "MESH"

        def _pick_mark(self, context, event):
            """光标附近最近的标记（屏幕距离 < 24px）→ index 或 None。"""
            obj = context.active_object
            marks = list(obj.get("vf_connect_points", []))
            if not marks:
                return None
            region = context.region
            rv3d = context.region_data
            if region is None or rv3d is None:
                return None
            from bpy_extras.view3d_utils import location_3d_to_region_2d
            best_i, best_d = None, 24.0
            # v31：标记是格空间坐标（世界米-location）——必须用只平移的
            # _grid_to_world_matrix 转世界；旧实现误用 matrix_world（再乘 scale）
            # → scale≠1 时拾取位置错位（点击标记选不中/选中别的标记）
            mw = _grid_to_world_matrix(obj)
            for i, m in enumerate(marks):
                w = mw @ Vector((m[0], m[1], m[2]))
                p2d = location_3d_to_region_2d(region, rv3d, w)
                if p2d is None:
                    continue
                d = ((p2d.x - event.mouse_region_x) ** 2 +
                     (p2d.y - event.mouse_region_y) ** 2) ** 0.5
                if d < best_d:
                    best_d, best_i = d, i
            return best_i

        def invoke(self, context, event):
            obj = context.active_object
            if obj is None or obj.type != "MESH":
                self.report({"ERROR"}, "请选中一个网格对象")
                return {"CANCELLED"}
            idx = self._pick_mark(context, event)
            if idx is None:
                self.report({"WARNING"}, "请点击一个连接点标记（绿色整面）再拖动")
                return {"CANCELLED"}
            marks = obj["vf_connect_points"]
            mark = marks[idx]
            cf = mark_to_cell_face(mark)
            self.obj = obj
            self.index = idx
            self.cell = (cf[0], cf[1], cf[2])  # 钳制基准（全程不变）
            self.face = cf[3]
            self.cells = _occupied_cells(obj)
            self.last_mouse = (event.mouse_region_x, event.mouse_region_y)
            context.window_manager.modal_handler_add(self)
            self.report({"INFO"},
                        f"面内调整：{cf[3]} 面 cell{cf[0]},{cf[1]},{cf[2]}"
                        f"（拖动=面内移动，ESC/右键=完成）")
            return {"RUNNING_MODAL"}

        def modal(self, context, event):
            if event.type in {"ESC", "RIGHTMOUSE"} or                     (event.type == "LEFTMOUSE" and event.value == "RELEASE"):
                self.report({"INFO"}, "面内调整完成")
                return {"FINISHED"}
            if event.type == "MOUSEMOVE":
                dx = event.mouse_region_x - self.last_mouse[0]
                dy = event.mouse_region_y - self.last_mouse[1]
                self.last_mouse = (event.mouse_region_x, event.mouse_region_y)
                if dx == 0 and dy == 0:
                    return {"RUNNING_MODAL"}
                rv3d = context.region_data
                if rv3d is None:
                    return {"RUNNING_MODAL"}
                import mathutils
                rot = rv3d.view_rotation.to_matrix()
                if rv3d.view_perspective == "ORTHO":
                    scale = rv3d.view_distance / max(context.region.height, 1)
                else:
                    scale = rv3d.view_distance * 0.75 / max(context.region.height, 1)
                right = mathutils.Vector((rot[0][0], rot[1][0], rot[2][0]))
                up = mathutils.Vector((rot[0][1], rot[1][1], rot[2][1]))
                delta = (right * dx + up * -dy) * scale
                marks = self.obj["vf_connect_points"]
                m = marks[self.index]
                moved = (m[0] + delta.x, m[1] + delta.y, m[2] + delta.z,
                         m[3], m[4], m[5])
                clamped = clamp_mark_to_face(moved, self.cells, cell=self.cell)
                marks[self.index] = clamped
            return {"RUNNING_MODAL"}

    class VF_OT_ExportRON(Operator):
        bl_idname = "voxelforge.export_ron"
        bl_label = "导出模块 RON"
        bl_description = ("选中对象 → 生成 ModuleDef RON（dims 自动按体积取整 + "
                          "面标记→连接点；无面标记时回退 MP_ 收集）")

        out_dir: StringProperty(name="输出目录", default="//",
                                subtype="DIR_PATH")
        export_glb: BoolProperty(
            name="同时导出 GLB", default=True,
            description="RON 导出时一并导出 GLB 到 <out>/models/<corp>/——游戏直接加载"
                        "（2026-08-19 用户：'网格有了之后怎么传到游戏里'）")

        def execute(self, context):
            obj = context.active_object
            if obj is None or obj.type != "MESH":
                self.report({"ERROR"}, "请选中一个网格对象")
                return {"CANCELLED"}
            dims = dims_from_bounds(_bounds_of(obj))
            # 面标记（唯一数据源——2026-08-21 重构）→ 连接点
            face_marks = obj.get("vf_connect_points", [])
            if not face_marks:
                self.report({"ERROR"},
                            "没有连接点——用'游戏'工具点面标记，或'自动生成连接点'")
                return {"CANCELLED"}
            mps = mount_points_from_face_marks(face_marks, dims)
            if not mps:
                self.report({"ERROR"}, "面标记全部越界（先对齐网格）")
                return {"CANCELLED"}
            dims_err = validate_dims(dims)
            if dims_err:
                self.report({"ERROR"}, dims_err)
                return {"CANCELLED"}
            mesh_err = validate_mesh_in_dims(obj, dims)
            if mesh_err:
                self.report({"ERROR"}, mesh_err)
                return {"CANCELLED"}
            ok, errors = validate_mount_points(mps, dims)
            if not ok:
                self.report({"ERROR"}, "; ".join(errors))
                return {"CANCELLED"}
            corp = context.scene.vf_export_corp
            module_id = module_id_from_name(obj.name, corp)
            ron = export_module_ron(
                module_id=module_id,
                name=obj.name,
                corp=corp,
                category=category_from_name(obj.name),
                mass=context.scene.vf_export_mass,
                hp=context.scene.vf_export_hp,
                dims=dims,
                mount_points=mps,
                model_path=f"models/{corp}/{module_id}.glb",
                tags=[context.scene.vf_export_tags] if context.scene.vf_export_tags else [],
            )
            out_dir = self.out_dir
            if out_dir.startswith("//"):
                out_dir = os.path.join(os.path.dirname(bpy.data.filepath or ""),
                                       out_dir[2:]) or os.getcwd()
            os.makedirs(out_dir, exist_ok=True)
            path = os.path.join(out_dir, f"{module_id}.ron")
            with open(path, "w", encoding="utf-8") as f:
                f.write(ron)
            # 一键 GLB（游戏 model_path 对应位置：<out>/models/<corp>/<id>.glb）
            if self.export_glb:
                glb_dir = os.path.join(out_dir, "models", corp)
                os.makedirs(glb_dir, exist_ok=True)
                glb_path = os.path.join(glb_dir, f"{module_id}.glb")
                # 记住选择状态——导出后恢复（2026-08-19：此前 select_all 清空用户选择）
                prev_active = context.active_object
                prev_sel = [o for o in bpy.data.objects if o.select_get()]
                try:
                    bpy.ops.object.select_all(action="DESELECT")
                    obj.select_set(True)
                    bpy.ops.export_scene.gltf(
                        filepath=glb_path,
                        export_format="GLB",
                        use_selection=True,
                    )
                    self.report({"INFO"}, f"已导出 {path} + GLB {glb_path}")
                except Exception as e:
                    self.report({"WARNING"}, f"RON 已导出 {path}，但 GLB 失败: {e}")
                finally:
                    bpy.ops.object.select_all(action="DESELECT")
                    for o in prev_sel:
                        o.select_set(True)
                    if prev_active is not None:
                        context.view_layer.objects.active = prev_active
            else:
                self.report({"INFO"}, f"已导出 {path}")
            return {"FINISHED"}

    class VF_OT_ExportAll(Operator):
        bl_idname = "voxelforge.export_all"
        bl_label = "批量导出"
        bl_description = "导出场景内全部网格对象为各自 RON"

        out_dir: StringProperty(name="输出目录", default="//", subtype="DIR_PATH")

        def execute(self, context):
            out_dir = self.out_dir
            if out_dir.startswith("//"):
                out_dir = os.path.join(os.path.dirname(bpy.data.filepath or ""),
                                       out_dir[2:]) or os.getcwd()
            os.makedirs(out_dir, exist_ok=True)
            corp = context.scene.vf_export_corp
            count, failed = 0, []
            for obj in bpy.data.objects:
                if obj.type != "MESH":
                    continue
                marks = obj.get("vf_connect_points", [])
                dims = dims_from_bounds(_bounds_of(obj))
                mps = mount_points_from_face_marks(marks, dims) if marks else []
                dims_err = validate_dims(dims)
                if dims_err:
                    failed.append(f"{obj.name}: {dims_err}")
                    continue
                ok, errors = validate_mount_points(mps, dims)
                if not ok:
                    failed.append(f"{obj.name}: {'; '.join(errors)}")
                    continue
                module_id = module_id_from_name(obj.name, corp)
                ron = export_module_ron(
                    module_id=module_id, name=obj.name, corp=corp,
                    category=category_from_name(obj.name),
                    mass=context.scene.vf_export_mass,
                    hp=context.scene.vf_export_hp, dims=dims,
                    mount_points=mps,
                    model_path=f"models/{corp}/{module_id}.glb",
                    tags=[],
                )
                with open(os.path.join(out_dir, f"{module_id}.ron"),
                          "w", encoding="utf-8") as f:
                    f.write(ron)
                count += 1
            msg = f"已导出 {count} 个"
            if failed:
                msg += f"，失败 {len(failed)}: {failed[:3]}"
            self.report({"INFO"}, msg)
            return {"FINISHED"}

    # ── 面板 ──
    class VF_PT_Main(bpy.types.Panel):
        bl_label = "VoxelForge"
        bl_idname = "VF_PT_Main"
        bl_space_type = "VIEW_3D"
        bl_region_type = "UI"
        bl_category = "VoxelForge"

        def draw(self, context):
            layout = self.layout
            scene = context.scene
            # 工具栏图标预览（诊断：图标数据是否有效——2026-08-19）
            if _GAME_ICON["value"]:
                row = layout.row()
                row.label(text="游戏工具图标:")
                row.template_icon(icon_value=_GAME_ICON["value"], scale=1.5)

            box = layout.box()
            box.label(text="体积网格（自动适配物体）")
            box.prop(scene, "vf_grid_show")
            box.prop(scene, "vf_blender_floor")

            box = layout.box()
            box.label(text="连接点（2026-08-22：大小可调）")
            box.prop(scene, "vf_connect_scale")
            # v31c：实时显示当前值（0.667 这类残留值曾让绿面看起来只有 2/3——
            # 用户截图像素实测 0.667/1.0 精确吻合，来源=文件保存的滑块值）
            box.label(text=f"当前连接点 = {scene.vf_connect_scale:.3f} 米/格"
                           f"（1.000=整格面 1×1 米；只影响显示）", icon="INFO")

            box = layout.box()
            box.label(text="网格对齐")
            box.operator("voxelforge.align_grid")
            box.operator("voxelforge.align_center")
            op = box.operator("voxelforge.scale_dims", text="按格数缩放…")
            box.prop(op, "scale_x")
            box.prop(op, "scale_y")
            box.prop(op, "scale_z")

            box = layout.box()
            box.label(text="连接点（2026-08-21 面调整规则）")
            box.label(text="只对外面有效 · 不超格子 · 默认在中间", icon="INFO")
            box.operator("voxelforge.gen_mp", text="自动生成连接点（主面每格 1 米）")
            box.operator("voxelforge.mark_selected_faces",
                         text="批量标记选中面（Edit Mode）")
            box.operator("voxelforge.face_adjust",
                         text="面内调整（点击标记后拖动）")
            box.operator("voxelforge.validate_mp", text="校验连接点（浮空/埋内部/冗余）")
            box.operator("voxelforge.fix_mp", text="一键修复（删除问题连接点）")
            box.operator("voxelforge.clear_face_marks", text="清除全部标记")

            box = layout.box()
            box.label(text="AI 一键管道（对齐→主面→导出）")
            op_ai = box.operator("voxelforge.auto_pipeline", text="AI 一键管道")
            box.prop(op_ai, "out_dir")

            box = layout.box()
            box.label(text="导出")
            box.prop(scene, "vf_export_corp")
            box.prop(scene, "vf_export_mass")
            box.prop(scene, "vf_export_hp")
            box.prop(scene, "vf_export_tags")
            op = box.operator("voxelforge.export_ron", text="导出模块 RON")
            box.prop(op, "out_dir")
            box.prop(op, "export_glb")
            op2 = box.operator("voxelforge.export_all", text="批量导出")
            box.prop(op2, "out_dir")

    def _register_scene_props():
        bpy.types.Scene.vf_export_corp = StringProperty(
            name="势力/corp", default="corp")
        bpy.types.Scene.vf_export_mass = FloatProperty(name="质量", default=10.0, min=0.1)
        bpy.types.Scene.vf_export_hp = IntProperty(name="HP", default=100, min=1)
        bpy.types.Scene.vf_export_tags = StringProperty(name="标签（逗号分隔）", default="")
        # 体积适配网格开关（2026-08-19 用户定案：网格=建模物体体积，
        # 有体积才有网格，无参数——用 Blender 缩放驱动；仅保留显示开关）
        bpy.types.Scene.vf_grid_show = BoolProperty(
            name="显示体积网格", default=True,
            description="显示建模物体体积的 3D 格框（1 米=1 格；选中物体优先，"
                        "否则所有网格物体；无物体不显示）")
        def _on_floor_toggle(self, context):
            _apply_floor_overlay()

        # Blender 自带地面网格：默认关——避免与游戏同款网格重叠混淆
        # （用户 2026-08-19："我要的网格可不是 blender 的那个网格"）
        bpy.types.Scene.vf_blender_floor = BoolProperty(
            name="Blender 自带网格", default=False,
            description="显示 Blender 自带地面网格（默认关——只留游戏同款网格）",
            update=_on_floor_toggle)
        # 连接点大小（2026-08-22 用户："放大的话连接点不会跟大小只会默认
        # 的在那个格子面里 当然可以在设置里面调整"——绿面默认占整格面，
        # 可调 0.3~3.0 倍（只影响渲染显示，导出 MountPoint 数据不变））
        bpy.types.Scene.vf_connect_scale = FloatProperty(
            name="连接点大小", default=1.0, min=0.3, max=3.0,
            description="连接点绿面相对格面的缩放（1.0=整格面；只影响显示，"
                        "导出 MountPoint 数据不变）")

    def _apply_floor_overlay():
        """同步所有 3D 视口的 Blender 自带地面网格开关（只留游戏同款网格）。"""
        try:
            scene = bpy.context.scene
        except Exception:
            return  # headless/无场景：跳过
        show = bool(getattr(scene, "vf_blender_floor", False))
        try:
            windows = bpy.context.window_manager.windows
        except Exception:
            return
        for window in windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    ov = area.spaces.active.overlay
                    if ov.show_floor != show:
                        ov.show_floor = show

    def _depsgraph_floor_sync(_depsgraph=None):
        """depsgraph 更新时：**缓存自愈**（2026-08-22 性能修复）。

        旧实现在 depsgraph 每次更新（鼠标移动/视口旋转/任何 ECS 变化）
        全清缓存 + 全量重算——点面时缓存永不命中 → 全量 BVH 遍历卡顿/
        延迟出现（用户"点面很慢才出现 / 各种卡顿"）。

        缓存 key = (name, 顶点数, 面数)——网格未变则 key 不变，自动命中；
        网格 changed 后 vcount/fcount 变化 → 旧条目自然失效。因此这里
        **不需要清**，只需在对象网格真正变化时失效（depsgraph update
        里的 object 变化事件监听）。本 handler 只同步 floor 开关。
        """
        _apply_floor_overlay()

    _REGISTER_CLASSES = [
        VF_OT_AlignGrid, VF_OT_AlignCenter, VF_OT_ScaleDims,
        VF_OT_GenMountPoints, VF_OT_ExportRON, VF_OT_ExportAll, VF_PT_Main,
        VF_OT_FaceConnectToggle, VF_OT_FaceConnectBatch, VF_OT_ClearFaceMarks,
        VF_OT_MarkSelectedFaces,
        VF_OT_ValidateMPs, VF_OT_FixMPs, VF_OT_FaceAdjust,
        VF_OT_AutoPipeline, VF_OT_ConnectExpand,
    ]

    def register():
        _register_scene_props()
        _apply_floor_overlay()  # 隐藏 Blender 自带地面网格——只留游戏同款
        # 持续同步（启动早期窗口未创建，register 时遍历不到；depsgraph 更新时补上）
        bpy.app.handlers.depsgraph_update_post.append(_depsgraph_floor_sync)
        for cls in _REGISTER_CLASSES:
            bpy.utils.register_class(cls)
        # 左侧工具栏"游戏"工具（底部，分隔线隔开）——图标=“游戏”两字
        _GAME_ICON["value"] = _build_game_icon()
        if _GAME_ICON["value"]:
            try:
                # 注入 icon 缓存：bl_icon="vf_game" → 绘制时命中自定义图标
                from bl_ui.space_toolsystem_common import _icon_cache
                _icon_cache["vf_game"] = _GAME_ICON["value"]
            except Exception as e:
                print(f"[voxelforge_connector] 图标注入失败: {e}")
        try:
            # 追加到工具栏最底部（不带 after——2026-08-19 实地调查发现：
            # after 会把工具插到 select 组后第 4 位，用户截图底部空白）
            bpy.utils.register_tool(
                VF_FaceConnectTool,
                separator=True,
            )
        except Exception as e:
            print(f"[voxelforge_connector] 工具注册失败: {e}")
        # 游戏网格 + 连接面高亮 draw handler
        global _VF_DRAW_HANDLER
        _VF_DRAW_HANDLER = bpy.types.SpaceView3D.draw_handler_add(
            _vf_draw_cb, (), "WINDOW", "POST_VIEW")

    def unregister():
        for cls in reversed(_REGISTER_CLASSES):
            bpy.utils.unregister_class(cls)
        # 移除持续同步 handler
        if _depsgraph_floor_sync in bpy.app.handlers.depsgraph_update_post:
            bpy.app.handlers.depsgraph_update_post.remove(_depsgraph_floor_sync)
        global _VF_DRAW_HANDLER
        if _VF_DRAW_HANDLER is not None:
            bpy.types.SpaceView3D.draw_handler_remove(_VF_DRAW_HANDLER, "WINDOW")
            _VF_DRAW_HANDLER = None
        if _GAME_ICON["value"]:
            try:
                bpy.app.icons.release(_GAME_ICON["value"])
            except Exception as e:
                print(f"[voxelforge_connector] 图标释放失败: {e}")
            _GAME_ICON["value"] = 0


# ════════════════════════════════════════════════════════════════════
# 三、无头批量导出 CLI（blender --background）
# ════════════════════════════════════════════════════════════════════

def cli_main():
    if not HAS_BPY or "--" not in sys.argv:
        return
    args = sys.argv[sys.argv.index("--") + 1:]
    # AI/headless 一键管道（2026-08-22）：自动对齐→主面→校验→导出
    if "--auto-pipeline" in args:
        idx = args.index("--auto-pipeline")
        out_dir = args[idx + 1] if idx + 1 < len(args) else os.getcwd()
        os.makedirs(out_dir, exist_ok=True)
        count, failed = 0, []
        for obj in [o for o in bpy.data.objects if o.type == "MESH"]:
            bpy.context.view_layer.objects.active = obj
            _auto_align_if_needed(obj)
            cells = _occupied_cells(obj)
            face, cells_on = primary_face_for_module(cells, _local_bounds(obj))
            if face is None:
                failed.append(f"{obj.name}: 无暴露面")
                continue
            marks = [face_mark_from_cell_face(g, face) for g in cells_on]  # 每格 1 点
            obj["vf_connect_points"] = merge_face_marks(
                [m for m in list(obj.get("vf_connect_points", []))
                 if mark_to_cell_face(m)[3] != face] + marks)
            dims = dims_from_bounds(_bounds_of(obj))
            mps = mount_points_from_face_marks(
                list(obj.get("vf_connect_points", [])), dims)
            ok, errors = validate_mount_points(mps, dims)
            if not ok:
                failed.append(f"{obj.name}: {'; '.join(errors)}")
                continue
            module_id = module_id_from_name(obj.name, "corp")
            ron = export_module_ron(
                module_id=module_id, name=obj.name, corp="corp",
                category=category_from_name(obj.name),
                mass=10.0, hp=100, dims=dims, mount_points=mps,
                model_path=f"models/corp/{module_id}.glb", tags=[],
            )
            with open(os.path.join(out_dir, f"{module_id}.ron"),
                      "w", encoding="utf-8") as f:
                f.write(ron)
            count += 1
        print(f"[voxelforge_connector] AI 管道导出 {count} 个 RON → {out_dir}")
        for err in failed:
            print(f"[voxelforge_connector] AI 管道失败: {err}")
        sys.exit(0 if not failed else 1)
    if "--export-all" not in args:
        return
    idx = args.index("--export-all")
    out_dir = args[idx + 1] if idx + 1 < len(args) else os.getcwd()
    corp = "corp"
    if "--corp" in args:
        corp = args[args.index("--corp") + 1]
    os.makedirs(out_dir, exist_ok=True)
    count, failed = 0, []
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        marks = obj.get("vf_connect_points", [])
        dims = dims_from_bounds(_bounds_of(obj))
        mps = mount_points_from_face_marks(marks, dims) if marks else []
        ok, errors = validate_mount_points(mps, dims)
        if not ok:
            failed.append(f"{obj.name}: {'; '.join(errors)}")
            continue
        module_id = module_id_from_name(obj.name, corp)
        ron = export_module_ron(
            module_id=module_id, name=obj.name, corp=corp,
            category=category_from_name(obj.name),
            mass=10.0, hp=100, dims=dims, mount_points=mps,
            model_path=f"models/{corp}/{module_id}.glb", tags=[],
        )
        with open(os.path.join(out_dir, f"{module_id}.ron"),
                  "w", encoding="utf-8") as f:
            f.write(ron)
        count += 1
    print(f"[voxelforge_connector] 导出 {count} 个 RON → {out_dir}")
    for err in failed:
        print(f"[voxelforge_connector] 失败: {err}")
    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    cli_main()
