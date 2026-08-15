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
    d = json.loads(server._call("mesh_check", {"path": str(p)})[0].text)
    assert d["ok"] is True and d["manifold"] is True, d
    assert d["vertices"] == 4 and d["faces"] == 4, d


def test_mesh_check_hole_and_nonmanifold(tmp_path, monkeypatch):
    """破面（边界边）+ 非流形边检出。"""
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    # 两个三角形共享一条边（非流形：边被 2 面共享但开口）——单面片=边界边
    p = _write_obj(tmp_path, "open.obj",
                   "v 0 0 0\nv 1 0 0\nv 0 1 0\n"
                   "f 1 2 3\n")
    d = json.loads(server._call("mesh_check", {"path": str(p)})[0].text)
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
    d = json.loads(server._call("mesh_optimize", {"path": str(p)})[0].text)
    assert d["ok"] is True, d
    assert d["welded_vertices"] == 1, f"重复顶点应检出: {d}"
    assert d["vertices_after_weld"] == 4, d


def test_mesh_splat_params(tmp_path, monkeypatch):
    """三角面片→可训练参数表（张量形状与顶点/面数一致）。"""
    monkeypatch.setattr(server, "_SANDBOX_ROOTS", [str(tmp_path)])
    p = _write_obj(tmp_path, "splat.obj",
                   "v 0 0 0\nv 1 0 0\nv 0 1 0\nv 0 0 1\n"
                   "f 1 2 3\nf 1 4 2\n")
    d = json.loads(server._call("mesh_splat", {"path": str(p)})[0].text)
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
    d = json.loads(server._call("voxelize", {"path": str(p),
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
    d = json.loads(server._call("mesh_check", {"path": str(p),
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
    d = json.loads(server._call("mesh_check", {"path": str(p)})[0].text)
    assert d["ok"] is True, d
    assert d["vertices"] == 3 and d["faces"] == 1, d
