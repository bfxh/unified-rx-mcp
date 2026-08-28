# -*- coding: utf-8 -*-
"""v31i 高压探针：点面=单格 toggle / 批量=鼠标扫过（方向自动）/ 性能

I1 点面=单格 toggle（点一格标一格；再点取消；点其它格共存）
I2 扫掠批量：路径格序列 → 实时逐格标记 + 方向轴判定
I3 方向：水平扫=左右(x)/垂直扫=前后(z)/斜扫=混合
I4 merge_face_marks 性能（400 格 <50ms——旧 O(N²)）
I5 重复格不重复计；离开模型后重新进入重新定向
I6 单格模型扫掠=1 格
"""
import bpy, sys, os, time
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

print("== I1: 点面=单格 toggle ==")
bpy.ops.wm.read_factory_settings(use_empty=True)
o = mk_box(2, 2, 2)
mark1 = vf.face_mark_from_cell_face((0, 1, 0), "Top")
o["vf_connect_points"] = [list(mark1)]
check("I1 点一格=1 个标记", len(o["vf_connect_points"]) == 1)
cf1 = vf.mark_to_cell_face(mark1)
marks = [m for m in o["vf_connect_points"] if vf.mark_to_cell_face(m) != cf1]
o["vf_connect_points"] = marks
check("I1 再点同格=取消（0 个）", len(o["vf_connect_points"]) == 0)
o["vf_connect_points"] = [list(vf.face_mark_from_cell_face(g, "Top"))
                          for g in ((0, 1, 0), (1, 1, 0))]
check("I1 点 2 个不同格=2 个标记", len(o["vf_connect_points"]) == 2)

print("== I2/I3: 扫掠批量 + 方向判定 ==")
bpy.ops.wm.read_factory_settings(use_empty=True)
o = mk_box(4, 4, 4)  # 顶面 4×4=16 格

class SweepSim:
    """扫掠模拟（与 batch operator 的 _sweep/_push_axis 同逻辑）"""
    def __init__(self, obj):
        self.obj = obj
        self.marks_before = list(obj.get("vf_connect_points", []))
        self.marked = 0
        self.last_cf = None
        self.first_axis = None
        self.axes = set()
        self.cells = vf._occupied_cells(obj)
        self.lb = vf._local_bounds(obj)
        self.air = vf.external_air_cells(self.cells, self.lb)
        self.occ_s = vf.occupied_set(self.cells)
        self.seen = {vf.mark_to_cell_face(m) for m in self.marks_before}

    def _push_axis(self, cf):
        if self.last_cf is None:
            self.last_cf = cf
            return
        dx = cf[0] - self.last_cf[0]
        dy = cf[1] - self.last_cf[1]
        dz = cf[2] - self.last_cf[2]
        nz = []
        if dx: nz.append("x")
        if dy: nz.append("y")
        if dz: nz.append("z")
        if not nz:
            self.last_cf = cf
            return
        if len(nz) > 1:
            self.axes.add("diag")
            self.last_cf = cf
            return
        ax = nz[0]
        self.axes.add(ax)
        if self.first_axis is None:
            self.first_axis = ax
        self.last_cf = cf

    def sweep(self, cf):
        if not vf.is_exposed_face(cf, self.cells, self.lb, self.air,
                                  self.occ_s):
            return
        if cf in self.seen:
            self._push_axis(cf)
            return
        self._push_axis(cf)
        self.seen.add(cf)
        marks = list(self.obj.get("vf_connect_points", []))
        marks.append(vf.face_mark_from_cell_face((cf[0], cf[1], cf[2]), cf[3]))
        self.obj["vf_connect_points"] = marks
        self.marked += 1

o["vf_connect_points"] = []
s = SweepSim(o)
for x in range(4):
    s.sweep((x, 3, 0, "Top"))
check("I2 扫过一行=4 格标记", s.marked == 4, f"got {s.marked}")
check("I3 水平扫过 → 方向=左右（x）", s.first_axis == "x",
      f"got {s.first_axis}")
o["vf_connect_points"] = []
s2 = SweepSim(o)
for z in range(4):
    s2.sweep((3, 3, z, "Top"))
check("I3 垂直扫过 → 方向=前后（z）", s2.first_axis == "z",
      f"got {s2.first_axis}")
o["vf_connect_points"] = []
s3 = SweepSim(o)
for i in range(4):
    s3.sweep((i, 3, i, "Top"))
check("I3 斜扫 → 斜向(diag=混合)", "diag" in s3.axes, f"got {s3.axes}")
o["vf_connect_points"] = []
s4 = SweepSim(o)
for _ in range(3):
    s4.sweep((0, 3, 0, "Top"))
check("I5 重复扫过同格=1 格", s4.marked == 1, f"got {s4.marked}")
o["vf_connect_points"] = []
s5 = SweepSim(o)
s5.sweep((0, 3, 0, "Top"))
s5.last_cf = None  # 离开模型清基准
s5.sweep((0, 3, 1, "Top"))
s5.sweep((0, 3, 2, "Top"))
check("I5 重新进入重新定向（首轴=z）", s5.first_axis == "z",
      f"got {s5.first_axis}")

print("== I4: merge_face_marks 性能（400 格）==")
marks = [vf.face_mark_from_cell_face((i % 20, 1, i // 20), "Top")
         for i in range(400)]
t = time.perf_counter()
merged = vf.merge_face_marks(marks)
dt = (time.perf_counter() - t) * 1000
check("I4 400 格 merge < 50ms", dt < 50, f"got {dt:.1f}ms")
check("I4 去重后 400（无重复）", len(merged) == 400, f"got {len(merged)}")
t = time.perf_counter()
merged2 = vf.merge_face_marks(marks + marks)
dt2 = (time.perf_counter() - t) * 1000
check("I4 800 输入（400 重复）< 80ms 且去重 400",
      dt2 < 80 and len(merged2) == 400, f"got {dt2:.1f}ms n={len(merged2)}")

print("== I6: 单格模型扫掠=1 格 ==")
bpy.ops.wm.read_factory_settings(use_empty=True)
o = mk_box(1, 1, 1)
s6 = SweepSim(o)
s6.sweep((0, 0, 0, "Top"))
check("I6 单格=1", s6.marked == 1, f"got {s6.marked}")

print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
bpy.ops.wm.quit_blender()
sys.exit(1 if FAIL else 0)
