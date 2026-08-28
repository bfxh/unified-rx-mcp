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


def merge_face_marks(marks, tolerance=0.05):
    """同格同向面标记合并为一个（复杂模型一格常被多个小面覆盖——斜切/
    细分曲面批量标记后去重）。方向按法线容差比较，位置按格容差比较。"""
    out = []
    for m in marks:
        dup = False
        for o in out:
            if (abs(m[0] - o[0]) < tolerance and
                    abs(m[1] - o[1]) < tolerance and
                    abs(m[2] - o[2]) < tolerance and
                    abs(m[3] - o[3]) < tolerance and
                    abs(m[4] - o[4]) < tolerance and
                    abs(m[5] - o[5]) < tolerance):
                dup = True
                break
        if not dup:
            out.append(m)
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
    """
    cx, cy, cz, nx, ny, nz = mark
    face = face_from_normal((nx, ny, nz))
    cell = (round(cx - nx * 0.5 - 0.5),
            round(cy - ny * 0.5 - 0.5),
            round(cz - nz * 0.5 - 0.5))
    if not (0 <= cell[0] < dims[0] and 0 <= cell[1] < dims[1] and 0 <= cell[2] < dims[2]):
        return None
    return (cell[0], cell[1], cell[2], face, 100.0, "Any", 0, (0.0, 0.0, 0.0), False)


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
    "blender": (5, 2, 0),
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

    # 占用格缓存（按 mesh 顶点/面数——mesh 编辑后自动失效；避免每帧 ray_cast）
    _GRID_CACHE = {}

    def _occupied_cells(obj):
        """物体占用格列表 [(cx, cy, cz, occupied)]——含空心检测。

        用户 2026-08-19："空心建模按格数，超过格子就在那增加一个格；
        空心就可以在那里放其他模块"——格 AABB 与 mesh 相交测试
        （BVHTree.overlap）：有相交面=占用（白框），无=空心（青框，可放模块）。
        偶奇法则弃用：相邻格重叠面使射线步进误判（2026-08-19 实测）。
        缓存 key = 顶点/面数（depsgraph 更新时统一清缓存）；大体积跳过检测。
        """
        import math
        import mathutils
        key = (obj.name, len(obj.data.vertices), len(obj.data.polygons))
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
        if dx * dy * dz <= 2048:
            try:
                from mathutils.bvhtree import BVHTree
                deps = bpy.context.evaluated_depsgraph_get()
                deps.update()  # 确保 BVHTree 用最新 mesh（顶点编辑/对齐后）
                bvh = BVHTree.FromObject(obj, deps)
                # 格=缩放空间；bvh=建模空间（bound_box 不含 scale）——
                # 检测坐标 ÷scale 换算（2026-08-21 修复）
                inv_sc = [1.0 / sc if abs(sc) > 1e-9 else 1e9
                          for sc in (obj.scale[0], obj.scale[1], obj.scale[2])]
                # 格子 AABB 内缩 0.01——共面接触不算相交（中心格贴着相邻
                # cube 但实际空心；2026-08-19 实测 overlap 共面误判占用）
                eps = 0.01
                axes = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
                # 格子的 8 顶点 + 12 三角面（两三角形/面）
                for cx in range(mx, mx + dx):
                    for cy in range(my, my + dy):
                        for cz in range(mz, mz + dz):
                            # 占用判定 = 三轴偶奇多数表决（格中心在 mesh 内部）
                            # OR overlap（格 AABB 内缩与 mesh 面相交）：
                            # - 实心格完全在内部：overlap 无面相交会漏——偶奇数
                            # - 相邻格重叠面使单轴偶奇误判——多数表决抵消
                            # - 空心格/中心空洞：偶奇外部 + overlap 0 → 空心
                            #   （2026-08-19 逐项实测定案）
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
                            x0, x1 = cx + eps, cx + 1.0 - eps
                            y0, y1 = cy + eps, cy + 1.0 - eps
                            z0, z1 = cz + eps, cz + 1.0 - eps
                            corners = [
                                (x0, y0, z0), (x1, y0, z0),
                                (x1, y1, z0), (x0, y1, z0),
                                (x0, y0, z1), (x1, y0, z1),
                                (x1, y1, z1), (x0, y1, z1),
                            ]
                            verts = [mathutils.Vector(
                                (c[0] * inv_sc[0], c[1] * inv_sc[1], c[2] * inv_sc[2]))
                                for c in corners]
                            faces = [
                                (0, 1, 2), (0, 2, 3),
                                (4, 5, 6), (4, 6, 7),
                                (0, 1, 5), (0, 5, 4),
                                (2, 3, 7), (2, 7, 6),
                                (0, 3, 7), (0, 7, 4),
                                (1, 2, 6), (1, 6, 5),
                            ]
                            cell_bvh = BVHTree.FromPolygons(verts, faces)
                            inter = len(bvh.overlap(cell_bvh)) > 0
                            cells.append((cx, cy, cz, inside or inter))
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

        # 逐格体积网格（用户 2026-08-19 定案："按格数；超过格子就在那增加
        # 一个；空心建模的空心处可以放其他模块"）：
        # - 选中 MESH → 该物体逐格绘制；无选中 → 所有 MESH 并集
        # - 占用格：白色 1×1×1 框；空心格（包围盒内无体积）：青色框=可放模块
        # - 无 MESH → 无网格
        occ_lines = []
        hole_lines = []
        exposed_lines = []
        if grid_show:
            objs = []
            ao = bpy.context.active_object
            if ao is not None and ao.type == "MESH":
                objs = [ao]
            else:
                objs = [o for o in bpy.data.objects if o.type == "MESH"]
            for obj in objs:
                cells = _occupied_cells(obj)
                occ = {(c[0], c[1], c[2]) for c in cells if c[3]}
                mw = obj.matrix_world
                for (cx, cy, cz, occupied) in cells:
                    if not occupied:
                        _cell_edges(cx, cy, cz, hole_lines, mw)
                        continue
                    # 暴露面可视化（2026-08-21）：格 6 面任一方向无占用格=
                    # 暴露（可连接）→ 亮青；全被挡=埋内部 → 白
                    if any((cx + dx, cy + dy, cz + dz) not in occ
                           for (_f, (dx, dy, dz)) in FACE_OFFSETS.items()):
                        _cell_edges(cx, cy, cz, exposed_lines, mw)
                    else:
                        _cell_edges(cx, cy, cz, occ_lines, mw)
        if exposed_lines:
            vbo = GPUVertBuf(fmt, len=len(exposed_lines))
            vbo.attr_fill(0, exposed_lines)
            batch = GPUBatch(type="LINES", buf=vbo)
            shader.bind()
            shader.uniform_float("color", (0.3, 0.95, 0.8, 0.9))
            batch.draw(shader)
        if occ_lines:
            vbo = GPUVertBuf(fmt, len=len(occ_lines))
            vbo.attr_fill(0, occ_lines)
            batch = GPUBatch(type="LINES", buf=vbo)
            shader.bind()
            shader.uniform_float("color", (1.0, 1.0, 1.0, 0.8))
            batch.draw(shader)
        if hole_lines:
            vbo = GPUVertBuf(fmt, len=len(hole_lines))
            vbo.attr_fill(0, hole_lines)
            batch = GPUBatch(type="LINES", buf=vbo)
            shader.bind()
            shader.uniform_float("color", (0.4, 0.9, 1.0, 0.9))
            batch.draw(shader)

        # 连接面标记：亮绿小方块（用户 2026-08-19："连接点的 UI 不如小方块
        # 来得好，连接的时候变大一些"——标记=网格格上的小立方体框，醒目）
        pts = []
        for obj in bpy.data.objects:
            if obj.type != "MESH" or not obj.get("vf_connect_points"):
                continue
            inv = obj.matrix_world
            for m in obj["vf_connect_points"]:
                cx, cy, cz, _nx, _ny, _nz = m
                w = inv @ mathutils.Vector((cx, cy, cz))
                s = 0.30  # 连接点小方块半长（连接时醒目）
                cs = [
                    [w.x - s, w.y - s, w.z - s], [w.x + s, w.y - s, w.z - s],
                    [w.x + s, w.y + s, w.z - s], [w.x - s, w.y + s, w.z - s],
                    [w.x - s, w.y - s, w.z + s], [w.x + s, w.y - s, w.z + s],
                    [w.x + s, w.y + s, w.z + s], [w.x - s, w.y + s, w.z + s],
                ]
                for (a, b) in [(0, 1), (1, 2), (2, 3), (3, 0),
                               (4, 5), (5, 6), (6, 7), (7, 4),
                               (0, 4), (1, 5), (2, 6), (3, 7)]:
                    pts.append(cs[a])
                    pts.append(cs[b])
        if pts:
            vbo2 = GPUVertBuf(fmt, len=len(pts))
            vbo2.attr_fill(0, pts)
            batch2 = GPUBatch(type="LINES", buf=vbo2)
            shader.uniform_float("color", (0.25, 0.95, 0.35, 1.0))
            batch2.draw(shader)


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
        ),)
        bl_operator = "voxelforge.face_connect_toggle"

        @classmethod
        def draw_settings(cls, context, layout, tool):
            obj = context.active_object
            if obj is None:
                layout.label(text="选择一个模型")
                return
            marks = obj.get("vf_connect_points", [])
            layout.label(text=f"已标记 {len(marks)} 个连接面")
            layout.label(text="点面=连接 / 再点=取消", icon="MOUSE_LMB")
            layout.operator("voxelforge.clear_face_marks", text="清除全部标记")

        @classmethod
        def draw_cursor(cls, context, tool, xy):
            # Blender 5.2 新签名：(context, tool, xy)——旧 (context, draw, x, y) 会
            # TypeError（2026-08-19 GUI 实地验证发现）。用 blf 画提示文字。
            try:
                import blf
                font_id = 0
                blf.size(font_id, 12)
                blf.position(font_id, xy[0] + 12, xy[1] + 12, 0)
                blf.color(font_id, 1.0, 1.0, 1.0, 1.0)
                blf.draw(font_id, "连接")
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
            # 面中心 + 法线（局部坐标）——网格编辑后仍可匹配
            if _face_idx < 0 or _face_idx >= len(obj.data.polygons):
                # ray_cast 索引针对评估网格（对象带修改器时与原始网格不一致）
                self.report({"WARNING"}, "面索引越界（对象带修改器？请应用修改器后重试）")
                return {"CANCELLED"}
            face_center = self._face_center_world(obj, _face_idx)
            inv = obj.matrix_world.inverted()
            center_local = inv @ face_center
            normal_local = inv.to_3x3() @ _norm
            # 吸附到网格格（用户 2026-08-19："连接始终是对着网格来搞的，
            # 不是对着模型"）：统一走 face_mark_from_geometry（公式单点维护）
            mark = face_mark_from_geometry(
                (center_local.x, center_local.y, center_local.z),
                (normal_local.x, normal_local.y, normal_local.z))
            marks = list(obj.get("vf_connect_points", []))
            # 切换：同一格同一面已存在 → 取消；否则 → 添加（按格判断）
            tolerance = 0.05
            for i, m in enumerate(marks):
                if (abs(m[0] - mark[0]) < tolerance and
                        abs(m[1] - mark[1]) < tolerance and
                        abs(m[2] - mark[2]) < tolerance):
                    del marks[i]
                    obj["vf_connect_points"] = marks
                    self.report({"INFO"}, "已取消该格连接（再点恢复）")
                    return {"FINISHED"}
            marks.append(mark)
            obj["vf_connect_points"] = marks
            self.report({"INFO"}, f"已标记连接格（共 {len(marks)} 个）")
            return {"FINISHED"}

        @staticmethod
        def _face_center_world(obj, face_index):
            import mathutils
            mesh = obj.data
            poly = mesh.polygons[face_index]
            coords = [mesh.vertices[v].co for v in poly.vertices]
            center = mathutils.Vector((0.0, 0.0, 0.0))
            for c in coords:
                center += c
            center /= len(coords)
            return obj.matrix_world @ center

    class VF_OT_ClearFaceMarks(Operator):
        """清除选中对象的全部面标记"""

        bl_idname = "voxelforge.clear_face_marks"
        bl_label = "清除连接标记"
        bl_options = {"REGISTER", "UNDO"}

        def execute(self, context):
            obj = context.active_object
            if obj is not None and obj.get("vf_connect_points"):
                del obj["vf_connect_points"]
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
                for v in obj.data.vertices:
                    v.co -= local_min
                # 2) 对象位置 = 世界最小角（向下取整落整数格）
                obj.location = Vector((int(b[0]), int(b[1]), int(b[2])))
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
            self.report({"INFO"}, "已按格数缩放")
            return {"FINISHED"}

    # ── 连接点 ──
    class VF_OT_GenMountPoints(Operator):
        """自动生成连接点（2026-08-21 重构：唯一方案 = 暴露面）。

        按实际占用格生成**面标记**（vf_connect_points——与点击面产生的
        标记同构、默认在格面中间）；只对外面有效：空气格/埋内部自动跳过。
        """
        bl_idname = "voxelforge.gen_mp"
        bl_label = "自动生成连接点"
        bl_description = "按实际占用格的暴露面生成连接点（只对外面有效，默认在中间）"

        def execute(self, context):
            obj = context.active_object
            if obj is None or obj.type != "MESH":
                self.report({"ERROR"}, "请选中一个网格对象")
                return {"CANCELLED"}
            cells = _occupied_cells(obj)
            mps = mount_points_for_occupied(cells, "exposed")
            marks = [face_mark_from_cell_face((m[0], m[1], m[2]), m[3]) for m in mps]
            old = list(obj.get("vf_connect_points", []))
            merged = merge_face_marks(old + marks)
            obj["vf_connect_points"] = merged
            self.report({"INFO"},
                        f"已生成 {len(marks)} 个连接点（暴露面；共 {len(merged)} 个标记）")
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
            inv = obj.matrix_world.inverted()
            inv_rot = inv.to_3x3()
            marks = list(obj.get("vf_connect_points", []))
            new_marks = []
            for f in selected:
                center = f.calc_center_median()
                normal = f.normal
                center_local = inv @ center
                normal_local = inv_rot @ normal
                new_marks.append(face_mark_from_geometry(
                    (center_local.x, center_local.y, center_local.z),
                    (normal_local.x, normal_local.y, normal_local.z)))
            merged = merge_face_marks(new_marks)
            before = len(marks)
            marks = merge_face_marks(marks + merged)
            obj["vf_connect_points"] = marks
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
            inv = obj.matrix_world
            for i, m in enumerate(marks):
                w = inv @ Vector((m[0], m[1], m[2]))
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
                self.report({"WARNING"}, "请点击一个连接点标记（黄色小方块）再拖动")
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
            box.operator("voxelforge.gen_mp", text="自动生成连接点（暴露面）")
            box.operator("voxelforge.mark_selected_faces",
                         text="批量标记选中面（Edit Mode）")
            box.operator("voxelforge.face_adjust",
                         text="面内调整（点击标记后拖动）")
            box.operator("voxelforge.validate_mp", text="校验连接点（浮空/埋内部/冗余）")
            box.operator("voxelforge.fix_mp", text="一键修复（删除问题连接点）")
            box.operator("voxelforge.clear_face_marks", text="清除全部标记")

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
        """depsgraph 更新时：清占用格缓存 + 预热重算（draw 回调里不能
        deps.update——这里安全）+ 同步 floor 开关。"""
        _GRID_CACHE.clear()
        try:
            ao = bpy.context.active_object
            objs = [ao] if (ao is not None and ao.type == "MESH") else \
                [o for o in bpy.data.objects if o.type == "MESH"]
            for o in objs:
                _occupied_cells(o)
        except Exception:
            pass
        _apply_floor_overlay()

    _REGISTER_CLASSES = [
        VF_OT_AlignGrid, VF_OT_AlignCenter, VF_OT_ScaleDims,
        VF_OT_GenMountPoints, VF_OT_ExportRON, VF_OT_ExportAll, VF_PT_Main,
        VF_OT_FaceConnectToggle, VF_OT_ClearFaceMarks, VF_OT_MarkSelectedFaces,
        VF_OT_ValidateMPs, VF_OT_FixMPs, VF_OT_FaceAdjust,
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
