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
