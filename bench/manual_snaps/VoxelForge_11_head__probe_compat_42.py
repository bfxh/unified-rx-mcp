# -*- coding: utf-8 -*-
"""Blender 4.2 兼容探针（v31g）：在 4.2 上运行——插件加载/注册/核心链路。

用法（Blender 4.2）：
  blender --background --python probe_compat_42.py

检查项：
C1 模块导入 + HAS_BPY
C2 bl_info.blender 最低版本 4.2（4.2 会拒绝更高声明）
C3 register()/unregister() 全量（4.2 API 差异在此爆炸）
C4 draw_cursor 兼容签名（旧 (context, draw, x, y) / 新 (context, tool, xy)）
C5 核心链路（占格/主面/标记/导出 RON）
C6 版本敏感 API 探测表
"""
import bpy
import sys
import os

PASS = 0
FAIL = 0

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")

print(f"[comp42] Blender {bpy.app.version_string}")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importlib.util
spec = importlib.util.spec_from_file_location(
    "vf", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "voxelforge_connector.py"))
vf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vf)

check("C1 模块导入 + HAS_BPY", vf.HAS_BPY)
check("C2 bl_info.blender 最低版本 4.2", vf.bl_info["blender"] == (4, 2, 0))

print("[comp42] 注册……")
try:
    vf.register()
    check("C3 register() 无异常", True)
except Exception as e:
    check("C3 register() 无异常", False, str(e))

try:
    vf.unregister()
    check("C3b unregister() 无异常", True)
except Exception as e:
    check("C3b unregister() 无异常", False, str(e))

# C4 draw_cursor 签名兼容（静态检查：*args 同时接受新旧两种签名——
# 实际调用需真实工具 context，headless 手动调会触 C 层崩溃（blf/gpu 无环境）
import inspect
try:
    sig = inspect.signature(vf.VF_FaceConnectTool.draw_cursor)
    params = list(sig.parameters.values())
    has_varargs = any(p.kind == p.VAR_POSITIONAL for p in params)
    check("C4 draw_cursor 签名 *args（新旧签名兼容）", has_varargs,
          f"got {sig}")
except Exception as e:
    check("C4 draw_cursor 签名 *args（新旧签名兼容）", False, str(e))

# C5 核心链路
bpy.ops.wm.read_factory_settings(use_empty=True)
mesh = bpy.data.meshes.new("c")
verts = [(x, y, z) for x in range(2) for y in range(2) for z in range(2)]
faces = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 2, 6, 4),
         (1, 5, 7, 3), (0, 4, 5, 1), (2, 3, 7, 6)]
mesh.from_pydata(verts, [], faces)
mesh.update()
obj = bpy.data.objects.new("c", mesh)
bpy.context.scene.collection.objects.link(obj)
bpy.context.view_layer.update()
try:
    cells = vf._occupied_cells(obj)
    occ = vf.occupied_set(cells)
    face, on = vf.primary_face_for_module(cells, vf._local_bounds(obj))
    marks = [vf.face_mark_from_cell_face(g, face) for g in on]
    mps = vf.mount_points_from_face_marks(marks, (2, 2, 2))
    ron = vf.export_module_ron("corp.x", "x", "corp", "Structure", 10.0, 100,
                               (2, 2, 2), mps, "models/corp/corp.x.glb")
    check("C5 占格 1（1×1×1 单盒）", len(occ) == 1, f"got {len(occ)}")
    check("C5 主面 Top 1 格 → 1 点", face == "Top" and len(on) == 1,
          f"got {face} {len(on)}")
    check("C5 RON 导出含 ModuleDef", "ModuleDef(" in ron)
    check("C5 挂点数量=1", ron.count("MountPoint(") == 1,
          f"got {ron.count('MountPoint(')}")
except Exception as e:
    import traceback
    traceback.print_exc()
    check("C5 核心链路", False, str(e))

# C6 版本敏感 API 探测表
import gpu
from mathutils.bvhtree import BVHTree
has = {
    "icons.new_triangles": hasattr(bpy.app.icons, "new_triangles"),
    "gpu.matrix.load_projection_matrix": hasattr(gpu.matrix,
                                                 "load_projection_matrix"),
    "BVHTree.overlap": hasattr(BVHTree, "overlap"),
    "tool_set_by_id": hasattr(bpy.ops.wm, "tool_set_by_id"),
    "register_tool": hasattr(bpy.utils, "register_tool"),
}
print(f"[comp42] 版本敏感 API: {has}")

print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
bpy.ops.wm.quit_blender()
sys.exit(1 if FAIL else 0)
