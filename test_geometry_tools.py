# -*- coding: utf-8 -*-
"""geometry_tools 测试（可微渲染+几何表示落地——4 工具确定性用例）。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402


def _write_obj(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def test_mesh_check_manifold(tmp_path, monkeypatch):
    """闭合流形网格 → 无拓扑问题（manifold=True）。"""
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    # 四面体（4 面闭合流形）
    p = _write_obj(tmp_path, "tet.obj",
                   "v 0 0 0\nv 1 0 0\nv 0 1 0\nv 0 0 1\n"
                   "f 1 2 3\nf 1 4 2\nf 1 3 4\nf 2 4 3\n")
    d = json.loads(server._call("mesh", {"action": "check", "path": str(p)})[0].text)
    assert d["ok"] is True and d["manifold"] is True, d
    assert d["vertices"] == 4 and d["faces"] == 4, d


def test_mesh_check_hole_and_nonmanifold(tmp_path, monkeypatch):
    """破面（边界边）+ 非流形边检出。"""
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    # 两个三角形共享一条边（非流形：边被 2 面共享但开口）——单面片=边界边
    p = _write_obj(tmp_path, "open.obj",
                   "v 0 0 0\nv 1 0 0\nv 0 1 0\n"
                   "f 1 2 3\n")
    d = json.loads(server._call("mesh", {"action": "check", "path": str(p)})[0].text)
    assert d["ok"] is True and d["manifold"] is False, d
    kinds = {i["kind"] for i in d["issues"]}
    assert "boundary_hole" in kinds, f"单面片应有边界边: {kinds}"
    assert d["issue_count"] >= 1, d


def test_mesh_optimize_weld(tmp_path, monkeypatch):
    """重复顶点 → welding 建议（精简率）。"""
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    # 4 顶点但有重复位置（v1 与 v5 同位置）
    p = _write_obj(tmp_path, "dup.obj",
                   "v 0 0 0\nv 1 0 0\nv 0 1 0\nv 0 0 1\nv 1 0 0\n"
                   "f 1 2 3\nf 1 4 2\nf 1 3 4\nf 2 4 3\n")
    d = json.loads(server._call("mesh", {"action": "optimize", "path": str(p)})[0].text)
    assert d["ok"] is True, d
    assert d["welded_vertices"] == 1, f"重复顶点应检出: {d}"
    assert d["vertices_after_weld"] == 4, d


def test_mesh_splat_params(tmp_path, monkeypatch):
    """三角面片→可训练参数表（张量形状与顶点/面数一致）。"""
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    p = _write_obj(tmp_path, "splat.obj",
                   "v 0 0 0\nv 1 0 0\nv 0 1 0\nv 0 0 1\n"
                   "f 1 2 3\nf 1 4 2\n")
    d = json.loads(server._call("mesh", {"action": "splat", "path": str(p)})[0].text)
    assert d["ok"] is True, d
    params = d["params"]
    assert params["vertex_tensor"]["shape"] == [4, 3], d
    assert params["face_index_tensor"]["shape"] == [2, 3], d
    assert params["normal_tensor"]["shape"] == [2, 3], d
    assert d["tensor_summary"]["param_count"] == 4 * 3 + 2 * 3, d
    assert "未来方向" in d["advice"], d


def test_voxelize_cube(tmp_path, monkeypatch):
    """立方体 → 体素占用正确（密度>0 且顶点在包围盒内）。"""
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    # 单位立方体（8 顶点 12 面）
    vs = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0),
          (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)]
    body = "".join(f"v {x} {y} {z}\n" for x, y, z in vs)
    quads = [(0, 1, 2, 3), (4, 5, 6, 7), (0, 1, 5, 4),
             (2, 3, 7, 6), (0, 3, 7, 4), (1, 2, 6, 5)]
    for q in quads:
        body += f"f {q[0]+1} {q[1]+1} {q[2]+1}\nf {q[0]+1} {q[2]+1} {q[3]+1}\n"
    p = _write_obj(tmp_path, "cube.obj", body)
    d = json.loads(server._call("voxel", {"action": "voxelize", "path": str(p),
                                             "resolution": "8"})[0].text)
    assert d["ok"] is True, d
    assert d["resolution"] == 8, d
    assert d["occupied_voxels"] > 0, d
    assert d["bbox"]["min"] == [0, 0, 0], d


def test_mesh_check_repair(tmp_path, monkeypatch):
    """repair=True → 重复顶点自动合并输出修复数据（引擎即用）。"""
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    p = tmp_path / "dup2.obj"
    p.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nv 0 0 1\nv 1 0 0\n"
                 "f 1 2 3\nf 1 4 2\nf 1 3 4\nf 2 4 3\n", encoding="utf-8")
    d = json.loads(server._call("mesh", {"action": "check", "path": str(p),
                                               "repair": "true"})[0].text)
    assert d["ok"] is True, d
    assert d["repair"]["welded_vertices"] == 1, d
    assert d["repair"]["vertices_after"] == 4, d
    assert len(d["repair"]["vertices"]) == 4, d


def test_glb_parse(tmp_path, monkeypatch):
    """GLB 二进制解析（构造最小 GLB——JSON chunk + BIN chunk）。"""
    import struct as _st
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    # 构造：1 三角形（3 顶点 POSITION + 3 索引）
    verts = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
    idx = [0, 1, 2]
    bin_data = _st.pack("<9f", *[c for v in verts for c in v]) + \
               _st.pack("<3H", *idx)
    gltf = {
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": 3,
             "type": "VEC3"},
            {"bufferView": 1, "componentType": 5123, "count": 3,
             "type": "SCALAR"},
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": 36},
            {"buffer": 0, "byteOffset": 36, "byteLength": 6},
        ],
        "buffers": [{"byteLength": 42}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0},
                                    "indices": 1}]}],
    }
    json_chunk = json.dumps(gltf).encode("utf-8")
    pad_json = (4 - len(json_chunk) % 4) % 4
    json_chunk += b" " * pad_json
    pad_bin = (4 - len(bin_data) % 4) % 4
    bin_data += b"\x00" * pad_bin
    header = b"glTF" + _st.pack("<II", 2, 12 + 8 + len(json_chunk) + 8 + len(bin_data))
    glb = header + _st.pack("<II", len(json_chunk), 0x4E4F534A) + json_chunk + \
          _st.pack("<II", len(bin_data), 0x004E4942) + bin_data
    p = tmp_path / "mini.glb"
    p.write_bytes(glb)
    d = json.loads(server._call("mesh", {"action": "check", "path": str(p)})[0].text)
    assert d["ok"] is True, d
    assert d["vertices"] == 3 and d["faces"] == 1, d


def test_geometry_exchange_obj_to_ply(tmp_path, monkeypatch):
    """Rhino.Inside 概念：OBJ → PLY 直接交换（无中间文件——内容输出）。"""
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    p = tmp_path / "src.obj"
    p.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
    d = json.loads(server._call("geometry_exchange", {
        "path": str(p), "target_format": "ply"})[0].text)
    assert d["ok"] is True, d
    assert "ply" in d["content"] and "element vertex 3" in d["content"], d
    assert d["vertices"] == 3 and d["faces"] == 1, d
    # OBJ → STL（base64 内容）
    d2 = json.loads(server._call("geometry_exchange", {
        "path": str(p), "target_format": "stl"})[0].text)
    assert d2["ok"] is True and d2["bytes"] > 80, d2


def test_half_edge_manifold(tmp_path, monkeypatch):
    """Manifold3D 概念：四面体半边结构（流形 + 无边界 + 1-ring）。"""
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    p = tmp_path / "tet2.obj"
    p.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nv 0 0 1\n"
                 "f 1 2 3\nf 1 4 2\nf 1 3 4\nf 2 4 3\n", encoding="utf-8")
    d = json.loads(server._call("half_edge", {"action": "analyze", "path": str(p)})[0].text)
    assert d["ok"] is True and d["manifold"] is True, d
    assert d["boundary_edges"] == 0, d
    assert d["half_edges"] == 12, d  # 4 面 × 3 半边
    assert d["sample_1_rings"], d


def test_half_edge_boundary(tmp_path, monkeypatch):
    """单面片 → 边界边检出（破面——边界 3 条）。"""
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    p = tmp_path / "open2.obj"
    p.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
    d = json.loads(server._call("half_edge", {"action": "analyze", "path": str(p)})[0].text)
    assert d["ok"] is True and d["manifold"] is False, d
    assert d["boundary_edges"] == 3, d


def test_mesh_union_weld(tmp_path, monkeypatch):
    """PicoGK 概念：两网格并集合并（跨网格顶点焊接）。"""
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    a = tmp_path / "a.obj"
    a.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
    b = tmp_path / "b.obj"
    # 与 a 共享顶点 0（位置 0,0,0）——焊接后总顶点 = 3 + 2 = 5
    b.write_text("v 0 0 0\nv 0 0 1\nv 1 0 1\nf 1 2 3\n", encoding="utf-8")
    d = json.loads(server._call("mesh", {"action": "union",
        "paths": [str(a), str(b)]})[0].text)
    assert d["ok"] is True, d
    assert d["vertices"] == 5, f"共享顶点应焊接（3+3-1=5）: {d}"
    assert d["faces"] == 2, d
    assert d["meshes"] == ["a.obj", "b.obj"], d


def test_mesh_clip_split(tmp_path, monkeypatch):
    """真·CSG 基础：四面体平面裁剪——跨平面顶点分裂 + 保留侧。"""
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    p = tmp_path / "tet3.obj"
    p.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nv 0 0 1\n"
                 "f 1 2 3\nf 1 4 2\nf 1 3 4\nf 2 4 3\n", encoding="utf-8")
    d = json.loads(server._call("mesh", {"action": "clip",
        "path": str(p), "plane": [0, 0, 1, -0.3]})[0].text)
    assert d["ok"] is True, d
    assert d["split_vertices"] >= 2, f"跨平面应有顶点分裂: {d}"
    assert d["faces"] >= 3, d
    # 全部在平面另一侧 → 完全丢弃（差集）
    d2 = json.loads(server._call("mesh", {"action": "clip",
        "path": str(p), "plane": [0, 0, 1, -2.0]})[0].text)
    assert d2["ok"] is True and d2["faces"] == 0, d2


def test_geom_graph_chain(tmp_path, monkeypatch):
    """Grasshopper 概念：节点图链（load → union → exchange）执行。"""
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    a = tmp_path / "ga.obj"
    a.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
    d = json.loads(server._call("geom", {"action": "graph",
        "nodes": [
            {"id": "src", "type": "load", "args": {"path": str(a)}},
            {"id": "out", "type": "exchange",
             "args": {"ref": "src", "target_format": "ply"}},
        ],
        "outputs": ["src", "out"]})[0].text)
    assert d["ok"] is True, d
    assert "src" in d["outputs"] and d["outputs"]["src"]["ok"] is True, d
    assert "ply" in d["outputs"]["out"]["content"], d
    # 非法节点类型拒绝
    d2 = json.loads(server._call("geom", {"action": "graph",
        "nodes": [{"id": "x", "type": "bogus", "args": {}}],
        "outputs": []})[0].text)
    assert d2["ok"] is False and "非法类型" in d2["error"], d2


def test_geom_example_generates(tmp_path):
    """PicoGK Program.cs 概念：三种示例代码生成（可直接运行）。"""
    for kind in ("union", "clip", "graph"):
        d = json.loads(server._call("geom", {"action": "example", "kind": kind})[0].text)
        assert d["ok"] is True and d["language"] == "python", d
        assert "import" in d["code"], d
        # union/clip 调用几何工具；graph 示例为节点 DSL（仅打印声明）
        if kind in ("union", "clip"):
            assert "geometry_tools" in d["code"], d
    # 非法 kind 拒绝
    d = json.loads(server._call("geom", {"action": "example", "kind": "bogus"})[0].text)
    assert d["ok"] is False, d


def test_half_edge_adjacency_api(tmp_path, monkeypatch):
    """升级：半边邻接查询 API（1-ring/关联面/边界——拓扑操控接口）。"""
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    p = tmp_path / "tet4.obj"
    p.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nv 0 0 1\n"
                 "f 1 2 3\nf 1 4 2\nf 1 3 4\nf 2 4 3\n", encoding="utf-8")
    d = json.loads(server._call("half_edge", {"action": "analyze", "action": "adjacency", "path": str(p), "vertex": 0})[0].text)
    assert d["ok"] is True and d["valency"] == 3, d
    assert d["neighbor_count"] == 3 and d["face_count"] == 3, d
    # 越界拒绝
    d2 = json.loads(server._call("half_edge", {"action": "analyze", "action": "adjacency", "path": str(p), "vertex": 99})[0].text)
    assert d2["ok"] is False and "越界" in d2["error"], d2


def test_mesh_boolean_relation(tmp_path, monkeypatch):
    """升级：CSG 布尔检测层（AABB 分离/相交/包含判定）。"""
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    a = tmp_path / "ba.obj"
    a.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
    far = tmp_path / "far.obj"  # AABB 分离（x=10）
    far.write_text("v 10 0 0\nv 11 0 0\nv 10 1 0\nf 1 2 3\n", encoding="utf-8")
    d = json.loads(server._call("mesh", {"action": "boolean", "paths": [str(a), str(far)]})[0].text)
    assert d["ok"] is True and d["relation"] == "separate", d
    # 相交（同一位置）
    d2 = json.loads(server._call("mesh", {"action": "boolean", "paths": [str(a), str(a)]})[0].text)
    assert d2["ok"] is True and d2["relation"] in ("overlapping", "contained"), d2



def test_mesh_boolean_empty_mesh_no_crash(tmp_path, monkeypatch):
    """security-review MEDIUM：空顶点网格（空 STL）不崩溃——返回结构化错误。"""
    import json as _j
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    a = tmp_path / "empty.stl"
    # 真 84 字节二进制空 STL（80 字节头 + 0 三角形）——触发空顶点路径，
    # 而非过短拒绝分支（27 字节文本 STL 在 parse 提前返回，测试假绿）
    import struct as _st
    a.write_bytes(bytes(80) + _st.pack("<I", 0))
    d = _j.loads(server._call("mesh", {"action": "boolean", "paths": [str(a), str(a)], "op": "intersect"})[0].text)
    assert d.get("ok") is False, f"空网格应返回错误而非崩溃: {d}"
    assert "error" in d, d

def test_voxel_surface_extract(tmp_path, monkeypatch):
    """升级：表面体素提取（表面点云——占用的子集）。"""
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    p = tmp_path / "cube.obj"
    p.write_text("v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\n"
                 "v 0 0 1\nv 1 0 1\nv 1 1 1\nv 0 1 1\n"
                 "f 1 2 3 4\nf 5 8 7 6\nf 1 5 6 2\nf 3 7 8 4\n"
                 "f 2 6 7 3\nf 1 4 8 5\n", encoding="utf-8")
    d = json.loads(server._call("voxel", {"action": "surface", "path": str(p), "resolution": 8})[0].text)
    assert d["ok"] is True and d["occupied"] > 0, d
    assert 0 < d["surface_voxels"] <= d["occupied"], d
    assert d["surface_points"], d


def test_load_mesh_cache_semantics(tmp_path, monkeypatch):
    """几何结果缓存（7 维缓存方案维度 4 安全落地，2026-08-19）：
    命中/深拷贝隔离/文件变更失效/失败不缓存。"""
    import geometry_tools as gt
    monkeypatch.setattr(gt, "_MESH_CACHE", {})
    p = tmp_path / "cube.obj"
    p.write_text("v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nf 1 2 3 4\n", encoding="utf-8")
    r1 = gt.load_mesh(str(p))
    assert r1["ok"] and len(r1["faces"]) == 2
    assert str(p) in gt._MESH_CACHE, "成功结果应入缓存"
    # 命中路径：直接对比结果等价
    r2 = gt.load_mesh(str(p))
    assert r2["ok"] and r2["vertices"] == r1["vertices"]
    # 深拷贝隔离：调用方污染返回结果不影响缓存
    r2["faces"].append("POLLUTED")
    r3 = gt.load_mesh(str(p))
    assert "POLLUTED" not in [f for f in r3["faces"] if isinstance(f, str)], "深拷贝隔离失效"
    # 文件变更（size 变）→ 缓存失效
    p.write_text("v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nv 2 0 0\nf 1 2 3 4\n", encoding="utf-8")
    r4 = gt.load_mesh(str(p))
    assert len(r4["vertices"]) == 5, "文件变更后应重新解析"
    # 失败结果不缓存
    gt._MESH_CACHE.clear()
    bad = gt.load_mesh(str(tmp_path / "missing.obj"))
    assert bad["ok"] is False and str(tmp_path / "missing.obj") not in gt._MESH_CACHE


def test_transform_compose_semantics(tmp_path, monkeypatch):
    """维度8（算子融合缓存）：TRS 合成语义 + 缓存命中 + 错误契约。"""
    import geometry_tools as gt
    monkeypatch.setattr(gt, "_TRANSFORM_CACHE", {})
    # glTF 惯例：[translate, rotate] = 先旋转后平移
    r = gt.transform_compose([
        {"type": "translate", "x": 1, "y": 0, "z": 0},
        {"type": "rotate", "axis": "z", "angle_deg": 90},
    ])
    assert r["ok"] and not r["cached"], r
    m = r["matrix"]
    # 点 (1,0,0)：先绕 z 转 90° → (0,1,0)，再平移 → (1,1,0)
    v = [1, 0, 0, 1]
    out = [round(sum(m[i * 4 + j] * v[j] for j in range(4)), 3) for i in range(4)]
    assert out[:3] == [1.0, 1.0, 0.0], f"TRS 合成语义错误: {out}"
    # 缓存命中（同参数）
    r2 = gt.transform_compose([
        {"type": "translate", "x": 1, "y": 0, "z": 0},
        {"type": "rotate", "axis": "z", "angle_deg": 90},
    ])
    assert r2["cached"] is True, "同参数应缓存命中"
    # 均匀缩放简写（只给 x）
    rs = gt.transform_compose([{"type": "scale", "x": 2}])
    assert rs["ok"] and rs["matrix"][0] == 2.0 and rs["matrix"][5] == 2.0
    # 错误契约
    assert gt.transform_compose([]).get("ok") is False
    assert gt.transform_compose([{"type": "nope"}]).get("ok") is False
    assert gt.transform_compose([{"type": "scale", "x": 0}]).get("ok") is False
    assert gt.transform_compose([{"type": "matrix", "m": [1] * 4}]).get("ok") is False


def test_pattern_expand_semantics(tmp_path, monkeypatch):
    """维度9（阵列展开缓存）：grid/ring/hilbert + 缓存 + 上限安全。"""
    import geometry_tools as gt
    monkeypatch.setattr(gt, "_PATTERN_CACHE", {})
    g = gt.pattern_expand("grid", rows=3, cols=2, spacing=1.0)
    assert g["ok"] and g["count"] == 6, g
    assert g["positions"][0] == (0.0, 0.0, 0.0)
    assert g["positions"][-1] == (1.0, 2.0, 0.0)  # (cols-1, rows-1)
    # 缓存命中
    g2 = gt.pattern_expand("grid", rows=3, cols=2, spacing=1.0)
    assert g2["cached"] is True and g2["positions"] == g["positions"]
    # ring 数量 = rows*cols
    rg = gt.pattern_expand("ring", rows=2, cols=3, spacing=1.0)
    assert rg["ok"] and rg["count"] == 6
    # hilbert 封顶（depth ≤ 6 → ≤64 段——防展开爆炸 DoS）
    hb = gt.pattern_expand("hilbert", rows=99, cols=99)
    assert hb["ok"] and hb["count"] <= 256, f"hilbert 应封顶: {hb['count']}"
    # 上限校验 + 错误契约
    assert gt.pattern_expand("grid", rows=999, cols=1).get("ok") is False
    assert gt.pattern_expand("grid", rows=2, cols=2, spacing=0).get("ok") is False
    assert gt.pattern_expand("nope", 2, 2).get("ok") is False


def _unit_cube(path):
    with open(path, "w", encoding="utf-8") as f:
        f.write("v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\n"
                "v 0 0 1\nv 1 0 1\nv 1 1 1\nv 0 1 1\n"
                "f 1 2 3 4\nf 5 8 7 6\nf 1 5 6 2\n"
                "f 3 7 8 4\nf 2 6 7 3\nf 1 4 8 5\n")


def test_mesh_bbox_and_mass_props(tmp_path, monkeypatch):
    """#5 包围盒缓存 + #76 质量属性缓存：单位立方体 → 体积 1.0、质心 0.5、
    表面积 6.0（#14/#15 neatmesh 指标）。"""
    import geometry_tools as gt
    monkeypatch.setattr(gt, "_BBOX_CACHE", {})
    monkeypatch.setattr(gt, "_MASS_CACHE", {})
    p = tmp_path / "cube.obj"
    _unit_cube(str(p))
    b = gt.mesh_bbox(str(p))
    assert b["ok"] and b["min"] == (0.0, 0.0, 0.0) and b["max"] == (1.0, 1.0, 1.0)
    b2 = gt.mesh_bbox(str(p))
    assert b2["min"] == b["min"], "包围盒应缓存命中"
    mp = gt.mesh_mass_props(str(p))
    assert mp["ok"], mp
    assert abs(mp["volume"] - 1.0) < 1e-4, f"单位立方体体积应≈1.0: {mp['volume']}"
    assert abs(mp["surface_area"] - 6.0) < 1e-4, f"单位立方体表面积应≈6.0: {mp['surface_area']}"
    assert abs(mp["surface_volume_ratio"] - 6.0) < 1e-3, f"表体比应≈6.0: {mp['surface_volume_ratio']}"
    assert all(abs(c - 0.5) < 1e-3 for c in mp["centroid"]), f"质心应≈0.5: {mp['centroid']}"
    mp2 = gt.mesh_mass_props(str(p))
    assert mp2["volume"] == mp["volume"], "质量属性应缓存命中"


def test_render_depth_loss_gradient(tmp_path, monkeypatch):
    """#9 可微渲染基础设施：软光栅→损失→数值梯度数据流 + 契约。"""
    import geometry_tools as gt
    monkeypatch.setattr(gt, "_MESH_CACHE", {})
    p = tmp_path / "cube.obj"
    _unit_cube(str(p))
    r = gt.render_depth(str(p), resolution=16)
    assert r["ok"] and len(r["depth"]) == 16, r
    # 全 0 目标 → 损失 1.0（渲染全 1）
    t0 = [[0.0] * 16 for _ in range(16)]
    l = gt.render_loss(str(p), t0, resolution=16)
    assert l["ok"] and abs(l["loss"] - 1.0) < 1e-6, f"loss 应≈1.0: {l}"
    # 全 1 目标 → 损失 0（渲染即目标）
    t1 = [[1.0] * 16 for _ in range(16)]
    l1 = gt.render_loss(str(p), t1, resolution=16)
    assert l1["ok"] and abs(l1["loss"]) < 1e-6, f"loss 应≈0: {l1}"
    # 数值梯度：3 分量有限（链路通畅）；顶点越界/eps 非法拒绝
    g = gt.render_gradient(str(p), t0, resolution=16, vertex=0, eps=0.05)
    assert g["ok"] and len(g["gradient"]) == 3, g
    assert all(isinstance(x, float) for x in g["gradient"])
    assert gt.render_gradient(str(p), t0, vertex=999).get("ok") is False
    assert gt.render_gradient(str(p), t0, eps=0).get("ok") is False
    assert gt.render_gradient(str(p), t0, eps=1.0).get("ok") is False


def test_voxelize_and_ray_cache(tmp_path, monkeypatch):
    """#18 体素化缓存 + #16 射线相交缓存：同参数命中、文件变更失效。"""
    import geometry_tools as gt
    monkeypatch.setattr(gt, "_VOXEL_CACHE", {})
    monkeypatch.setattr(gt, "_RAY_HIT_CACHE", {})
    p = tmp_path / "cube.obj"
    _unit_cube(str(p))
    v1 = gt.voxelize(str(p), resolution=8)
    assert v1["ok"] and v1["total_voxels"] == 512, v1
    assert len(gt._RAY_HIT_CACHE) > 0, "射线相交应有缓存条目"
    v2 = gt.voxelize(str(p), resolution=8)
    assert v2["occupied_voxels"] == v1["occupied_voxels"], "体素化应缓存命中"
    # 不同 resolution → 不同键
    v3 = gt.voxelize(str(p), resolution=16)
    assert v3["total_voxels"] == 4096
    # 文件变更 → 失效
    with open(str(p), "a", encoding="utf-8") as f:
        f.write("v 2 0 0\nv 2 1 0\n")
    v4 = gt.voxelize(str(p), resolution=8)
    assert v4["ok"], "文件变更后应重新计算"


def test_collision_check_modes(tmp_path, monkeypatch):
    """碰撞检测（FCL/Parry 概念零依赖）：mesh-mesh/点/包围盒四模式。"""
    import geometry_tools as gt
    monkeypatch.setattr(gt, "_TRI_TRI_CACHE", {})
    p1 = tmp_path / "a.obj"
    _unit_cube(str(p1))
    p2 = tmp_path / "b.obj"
    # 重叠立方体（+0.5 x）
    open(str(p2), "w", encoding="utf-8").write(
        open(str(p1), encoding="utf-8").read().replace("v 0 0 0", "v 0.5 0 0")
        .replace("v 1 0 0", "v 1.5 0 0").replace("v 1 1 0", "v 1.5 1 0")
        .replace("v 0 1 0", "v 0.5 1 0").replace("v 0 0 1", "v 0.5 0 1")
        .replace("v 1 0 1", "v 1.5 0 1").replace("v 1 1 1", "v 1.5 1 1")
        .replace("v 0 1 1", "v 0.5 1 1"))
    r = gt.collision_check(str(p1), str(p2))
    assert r["ok"] and r["colliding"] is True, f"重叠应碰撞: {r}"
    p3 = tmp_path / "c.obj"
    open(str(p3), "w", encoding="utf-8").write(
        open(str(p1), encoding="utf-8").read().replace("v 0 0 0", "v 3 0 0")
        .replace("v 1 0 0", "v 4 0 0").replace("v 1 1 0", "v 4 1 0")
        .replace("v 0 1 0", "v 3 1 0").replace("v 0 0 1", "v 3 0 1")
        .replace("v 1 0 1", "v 4 0 1").replace("v 1 1 1", "v 4 1 1")
        .replace("v 0 1 1", "v 3 1 1"))
    r2 = gt.collision_check(str(p1), str(p3))
    assert r2["ok"] and r2["colliding"] is False, f"分离不应碰撞: {r2}"
    assert r2["broad_phase"] == "separate"
    # mesh-point（中心点在内部——射线去重修复）
    r3 = gt.collision_check(str(p1), point=[0.5, 0.5, 0.5])
    assert r3["ok"] and r3["colliding"] is True, f"中心点应在内: {r3}"
    r4 = gt.collision_check(str(p1), point=[9, 9, 9])
    assert r4["colliding"] is False
    # mesh-aabb
    r5 = gt.collision_check(str(p1), aabb=[[0.2, 0.2, 0.2], [0.8, 0.8, 0.8]])
    assert r5["colliding"] is True
    r6 = gt.collision_check(str(p1), aabb=[[9, 9, 9], [10, 10, 10]])
    assert r6["colliding"] is False


def test_mesh_betti_semantics(tmp_path, monkeypatch):
    """#11 Betti 数（拓扑质量度量）：球面/双分量/开口网格。"""
    import geometry_tools as gt
    p = tmp_path / "cube.obj"
    _unit_cube(str(p))
    r = gt.mesh_betti(str(p))
    assert r["ok"] and r["closed"] is True
    assert r["betti"]["beta0"] == 1, f"单连通 β0=1: {r['betti']}"
    assert r["betti"]["beta2"] == 1, f"球面拓扑 β2=1: {r['betti']}"
    # 双分离立方体：β0=2（面索引偏移构造）
    vlines = [l for l in open(str(p), encoding="utf-8").read().splitlines()
              if l.startswith("v ")]
    flines = [l for l in open(str(p), encoding="utf-8").read().splitlines()
              if l.startswith("f ")]
    two = tmp_path / "two.obj"
    with open(str(two), "w", encoding="utf-8") as f:
        f.write(open(str(p), encoding="utf-8").read() + "\n")
        for l in vlines:
            parts = l.split()
            f.write(f"v {float(parts[1]) + 5} {parts[2]} {parts[3]}\n")
        for l in flines:
            idxs = [str(int(x) + 8) for x in l.split()[1:]]
            f.write("f " + " ".join(idxs) + "\n")
    r2 = gt.mesh_betti(str(two))
    assert r2["betti"]["beta0"] == 2, f"双分量 β0=2: {r2['betti']}"
    assert r2["betti"]["beta2"] == 2
    # 开口网格（少一个面）：非闭合 → β1/β2 标注不可靠
    hole = tmp_path / "hole.obj"
    open(str(hole), "w", encoding="utf-8").write(
        "\n".join(open(str(p), encoding="utf-8").read().splitlines()[:-1]) + "\n")
    r3 = gt.mesh_betti(str(hole))
    assert r3["ok"] and r3["closed"] is False and r3["boundary_edges"] > 0


def test_lbs_skin_deform_and_gradient(tmp_path, monkeypatch):
    """LBS 蒙皮（趋势：LBS→神经-物理可微分框架的零依赖数据基础设施）：
    单位矩阵不变形/平移骨跟随/混合插值/模板/权重归一化/梯度有限差分。"""
    import geometry_tools as gt
    monkeypatch.setattr(gt, "_SKIN_CACHE", {})
    p = tmp_path / "cube.obj"
    _unit_cube(str(p))
    I = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    T = [1, 0, 0, 10, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    # 1) 单位矩阵 + 刚性权重 → 不变形
    r = gt.skin_deform(str(p), [{"matrix": I}],
                       {"0": [{"bone": 0, "weight": 1.0}]})
    assert r["ok"] and r["deformed_vertices"][0] == (0.0, 0.0, 0.0)
    # 2) 平移骨 → 顶点跟随
    r2 = gt.skin_deform(str(p), [{"matrix": T}],
                        {"0": [{"bone": 0, "weight": 1.0}]})
    assert r2["ok"] and r2["deformed_vertices"][0] == (10.0, 0.0, 0.0)
    # 3) 混合权重 0.5/0.5（I 与 T 平移 10）→ 插值 5
    r3 = gt.skin_deform(str(p), [{"matrix": I}, {"matrix": T}],
                        {"0": [{"bone": 0, "weight": 0.5},
                               {"bone": 1, "weight": 0.5}]})
    assert r3["ok"] and r3["deformed_vertices"][0] == (5.0, 0.0, 0.0), r3
    # 4) 模板（#83 权重模板复用）
    r4 = gt.skin_deform(str(p), [{"matrix": I}, {"matrix": T}], {},
                        template="rigid")
    assert r4["ok"] and r4["template"] == "rigid"
    assert gt.skin_deform(str(p), [{"matrix": I}], {}, template="nope").get("ok") is False
    # 5) 权重归一化（2026-08-20 修复：Σw≠1 放大/缩水——1.5 应归一化到 1.0）
    rn = gt.skin_deform(str(p), [{"matrix": I}],
                        {"1": [{"bone": 0, "weight": 1.5}]})
    assert rn["ok"] and rn["deformed_vertices"][1] == (1.0, 0.0, 0.0), \
        f"权重 1.5 应归一化（非放大）: {rn['deformed_vertices'][1]}"
    rn2 = gt.skin_deform(str(p), [{"matrix": I}],
                         {"1": [{"bone": 0, "weight": 0.5}]})
    assert rn2["ok"] and rn2["deformed_vertices"][1] == (1.0, 0.0, 0.0), \
        f"权重 0.5 应归一化（非缩水）: {rn2['deformed_vertices'][1]}"
    assert gt.skin_deform(str(p), [{"matrix": I}],
                          {"1": [{"bone": 0, "weight": 0.0}]}).get("ok") is False, \
        "零权重和应拒绝"
    # 6) 梯度（有限差分 vs 归一化变形精确一致——-4.9505 实测）
    g0 = gt.skin_gradient(str(p), [{"matrix": I}, {"matrix": T}],
                          {"1": [{"bone": 0, "weight": 0.5},
                                 {"bone": 1, "weight": 0.5}]},
                          vertex=1, bone=0, eps=0.01)
    assert g0["ok"] and abs(g0["gradient"][0] - (-4.9505)) < 1e-2, f"bone0 梯度应≈-4.95: {g0}"
    g1 = gt.skin_gradient(str(p), [{"matrix": I}, {"matrix": T}],
                          {"1": [{"bone": 0, "weight": 0.5},
                                 {"bone": 1, "weight": 0.5}]},
                          vertex=1, bone=1, eps=0.01)
    assert g1["ok"] and abs(g1["gradient"][0] - 4.9505) < 1e-2, f"bone1 梯度应≈4.95: {g1}"
    # 7) 契约：骨骼越界/权重越界/eps 非法
    assert gt.skin_deform(str(p), [{"matrix": [1] * 4}],
                          {"0": [{"bone": 0, "weight": 1.0}]}).get("ok") is False
    assert gt.skin_gradient(str(p), [{"matrix": I}], {"0": [{"bone": 0, "weight": 1.0}]},
                            vertex=99).get("ok") is False
    assert gt.skin_gradient(str(p), [{"matrix": I}], {"0": [{"bone": 0, "weight": 1.0}]},
                            eps=0).get("ok") is False


def test_cache_isolation_all_caches(tmp_path, monkeypatch):
    """2026-08-20 挖漏洞修复：7 个缓存双向深拷贝——调用方污染返回结果
    不得影响后续命中（skin/voxel/bbox/mass/transform/pattern/mesh）。"""
    import geometry_tools as gt
    for c in (gt._SKIN_CACHE, gt._VOXEL_CACHE, gt._BBOX_CACHE, gt._MASS_CACHE,
              gt._TRANSFORM_CACHE, gt._PATTERN_CACHE):
        c.clear()
    p = tmp_path / "cube.obj"
    _unit_cube(str(p))
    I = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    # skin
    r1 = gt.skin_deform(str(p), [{"matrix": I}], {"1": [{"bone": 0, "weight": 1.0}]})
    r1["deformed_vertices"].append("P")
    assert "P" not in gt.skin_deform(str(p), [{"matrix": I}],
                                     {"1": [{"bone": 0, "weight": 1.0}]})["deformed_vertices"]
    # voxelize
    v1 = gt.voxelize(str(p), resolution=4)
    v1["bbox"]["min"] = "P"
    assert gt.voxelize(str(p), resolution=4)["bbox"]["min"] != "P"
    # bbox / mass
    b1 = gt.mesh_bbox(str(p)); b1["min"] = "P"
    assert gt.mesh_bbox(str(p))["min"] != "P"
    m1 = gt.mesh_mass_props(str(p)); m1["centroid"][0] = 999
    assert gt.mesh_mass_props(str(p))["centroid"][0] != 999
    # transform / pattern
    t1 = gt.transform_compose([{"type": "translate", "x": 1, "y": 0, "z": 0}])
    t1["matrix"][0] = 999
    assert gt.transform_compose([{"type": "translate", "x": 1, "y": 0, "z": 0}])["matrix"][0] != 999
    p1 = gt.pattern_expand("grid", rows=2, cols=2)
    p1["positions"].append("P")
    assert "P" not in gt.pattern_expand("grid", rows=2, cols=2)["positions"]
    # mesh（load_mesh 缓存）
    gt._MESH_CACHE.clear()
    m = gt.load_mesh(str(p))
    m["faces"].append("P")
    assert "P" not in gt.load_mesh(str(p))["faces"]


def test_hilbert_curve_correctness(tmp_path, monkeypatch):
    """2026-08-20 挖漏洞修复：Hilbert 曲线迭代实现——16/64 点唯一、
    相邻曼哈顿距离=1（空间填充性质）、depth 封顶 256。"""
    import geometry_tools as gt
    monkeypatch.setattr(gt, "_PATTERN_CACHE", {})
    h2 = gt.pattern_expand("hilbert", rows=2, cols=2)
    pts2 = h2["positions"]
    assert len(pts2) == 16 and len(set(pts2)) == 16, f"n=2 应 16 唯一点: {len(set(pts2))}"
    assert all(abs(pts2[i][0] - pts2[i - 1][0]) + abs(pts2[i][1] - pts2[i - 1][1]) == 1
               for i in range(1, len(pts2))), "n=2 相邻点曼哈顿距离必须=1"
    h3 = gt.pattern_expand("hilbert", rows=3, cols=3)
    assert len(h3["positions"]) == 64 and len(set(h3["positions"])) == 64
    h9 = gt.pattern_expand("hilbert", rows=99, cols=99)
    assert len(h9["positions"]) == 256, f"depth 封顶应 256: {len(h9['positions'])}"
