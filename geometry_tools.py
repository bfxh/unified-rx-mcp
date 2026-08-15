#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""geometry_tools —— 可微渲染与新一代几何表示落地（2026-08-15）。

概念映射（用户点名的前沿方向——务实落地，不交付假的）：
- mesh_check    ← TetSphere 概念：拓扑质量（破面/非流形天生问题检测）
- mesh_optimize ← 非经典 NURBS 概念：表示效率（多边形精简建议）
- mesh_splat    ← Triangle Splatting 概念：三角面片→可训练参数表
- voxelize      ← Radiant Foam 概念：体素表示（光线追踪分析基础）

零依赖（OBJ 文本 / STL 二进制 / PLY 文本——std lib 足够）。
真·GPU 可微渲染（梯度优化三角面片）标注为未来方向——本实现提供数据基础设施。
"""
import math
import os
import struct
from collections import defaultdict


# ── 解析层（OBJ / STL 二进制 / PLY 文本）───────────────────
def load_mesh(path: str) -> dict:
    """加载网格 → {vertices: [(x,y,z)], faces: [(i,j,k)], normals, uvs}。"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".obj":
        return _parse_obj(path)
    if ext == ".stl":
        return _parse_stl(path)
    if ext == ".ply":
        return _parse_ply(path)
    return {"ok": False, "error": f"不支持的格式: {ext}（支持 .obj/.stl/.ply）"}


def _parse_obj(path: str) -> dict:
    verts, faces, normals, uvs = [], [], [], []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if not parts:
                    continue
                if parts[0] == "v":
                    verts.append((float(parts[1]), float(parts[2]),
                                  float(parts[3])))
                elif parts[0] == "vn":
                    normals.append((float(parts[1]), float(parts[2]),
                                    float(parts[3])))
                elif parts[0] == "vt":
                    uvs.append((float(parts[1]), float(parts[2])))
                elif parts[0] == "f":
                    idx = []
                    for tok in parts[1:]:
                        i = tok.split("/")[0]
                        if i:
                            idx.append(int(i) - 1)
                    if len(idx) >= 3:
                        # 三角化（fan）
                        for k in range(1, len(idx) - 1):
                            faces.append((idx[0], idx[k], idx[k + 1]))
    except OSError as e:
        return {"ok": False, "error": str(e)}
    if not verts or not faces:
        return {"ok": False, "error": "OBJ 无顶点/面"}
    return {"ok": True, "format": "obj", "vertices": verts, "faces": faces,
            "normals": normals, "uvs": uvs}


def _parse_stl(path: str) -> dict:
    """二进制 STL：84 字节头 + 每三角形 50 字节（法线 12 + 顶点 36 + 属性 2）。"""
    verts, faces = [], []
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return {"ok": False, "error": str(e)}
    if len(data) < 84:
        return {"ok": False, "error": "STL 文件过短"}
    n = struct.unpack_from("<I", data, 80)[0]
    off = 84
    if len(data) < off + n * 50:
        return {"ok": False, "error": "STL 三角形数量与文件大小不符（可能文本 STL——暂不支持）"}
    for _ in range(n):
        tri = struct.unpack_from("<9fH", data, off)
        off += 50
        base = len(verts)
        for k in range(3):
            verts.append((tri[3 + k * 3], tri[4 + k * 3], tri[5 + k * 3]))
        faces.append((base, base + 1, base + 2))
    return {"ok": True, "format": "stl", "vertices": verts, "faces": faces,
            "normals": [], "uvs": []}


def _parse_ply(path: str) -> dict:
    """ASCII PLY：header + 顶点/面列表。"""
    verts, faces = [], []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        return {"ok": False, "error": str(e)}
    i = 0
    n_vert = n_face = 0
    in_header = True
    while i < len(lines):
        s = lines[i].strip()
        i += 1
        if in_header:
            if s.startswith("element vertex"):
                n_vert = int(s.split()[-1])
            elif s.startswith("element face"):
                n_face = int(s.split()[-1])
            elif s == "end_header":
                in_header = False
            continue
        break
    # 顶点
    for _ in range(n_vert):
        if i >= len(lines):
            break
        parts = lines[i].split()
        i += 1
        if len(parts) >= 3:
            try:
                verts.append((float(parts[0]), float(parts[1]),
                              float(parts[2])))
            except ValueError:
                continue
    # 面
    for _ in range(n_face):
        if i >= len(lines):
            break
        parts = lines[i].split()
        i += 1
        if parts and parts[0].isdigit():
            k = int(parts[0])
            idx = [int(x) for x in parts[1:1 + k]]
            if len(idx) >= 3:
                for m in range(1, len(idx) - 1):
                    faces.append((idx[0], idx[m], idx[m + 1]))
    if not verts or not faces:
        return {"ok": False, "error": "PLY 无顶点/面"}
    return {"ok": True, "format": "ply", "vertices": verts, "faces": faces,
            "normals": [], "uvs": []}


# ── ① mesh_check：拓扑质量（TetSphere 概念）───────────────
def mesh_check(path: str) -> dict:
    """拓扑健康报告：非流形边/破面（边界边）/孤立顶点/法线一致性。"""
    m = load_mesh(path)
    if not m.get("ok"):
        return m
    verts, faces = m["vertices"], m["faces"]
    # 边共享计数（无向边）
    edge_faces: dict[tuple, list] = defaultdict(list)
    for fi, (a, b, c) in enumerate(faces):
        for e in ((min(a, b), max(a, b)), (min(b, c), max(b, c)),
                  (min(a, c), max(a, c))):
            edge_faces[e].append(fi)
    boundary_edges = [e for e, fs in edge_faces.items() if len(fs) == 1]
    nonmanifold_edges = [e for e, fs in edge_faces.items() if len(fs) > 2]
    # 孤立顶点（不属任何面）
    used = set()
    for f in faces:
        used.update(f)
    isolated = [i for i in range(len(verts)) if i not in used]
    # 顶点重复（welding 候选）
    pos_map: dict[tuple, list] = defaultdict(list)
    for i, v in enumerate(verts):
        pos_map[(round(v[0], 4), round(v[1], 4), round(v[2], 4))].append(i)
    dup_verts = {i: idxs for i, idxs in pos_map.items() if len(idxs) > 1}
    issues = []
    if boundary_edges:
        issues.append({"kind": "boundary_hole",
                       "count": len(boundary_edges),
                       "detail": f"{len(boundary_edges)} 条边界边——"
                                 f"网格有破面/洞（TetSphere 概念：需流形闭合）"})
    if nonmanifold_edges:
        issues.append({"kind": "nonmanifold",
                       "count": len(nonmanifold_edges),
                       "detail": f"{len(nonmanifold_edges)} 条非流形边"
                                 f"（>2 面共享）——引擎渲染/物理会出问题"})
    if isolated:
        issues.append({"kind": "isolated_vertex", "count": len(isolated),
                       "detail": f"{len(isolated)} 个孤立顶点（浪费顶点数）"})
    if dup_verts:
        issues.append({"kind": "duplicate_vertices",
                       "count": len(dup_verts),
                       "detail": f"{len(dup_verts)} 组重复顶点位置——"
                                 f"welding 可精简（mesh_optimize）"})
    return {"ok": True, "path": path, "format": m["format"],
            "vertices": len(verts), "faces": len(faces),
            "edge_count": len(edge_faces),
            "manifold": not boundary_edges and not nonmanifold_edges,
            "issues": issues, "issue_count": len(issues),
            "advice": ("流形且无破面" if not issues else
                       "网格有拓扑问题——先用 mesh_optimize 修复再引擎使用")}


# ── ② mesh_optimize：表示效率（NURBS 概念）─────────────────
def mesh_optimize(path: str, target_ratio: float = 0.5) -> dict:
    """精简建议：重复顶点合并 + 共面面片合并候选 + 精简率评估。"""
    m = load_mesh(path)
    if not m.get("ok"):
        return m
    verts, faces = m["vertices"], m["faces"]
    # 重复顶点合并（welding）
    pos_map: dict[tuple, int] = {}
    remap = []
    for v in verts:
        key = (round(v[0], 4), round(v[1], 4), round(v[2], 4))
        if key not in pos_map:
            pos_map[key] = len(pos_map)
        remap.append(pos_map[key])
    unique_count = len(pos_map)
    welded = len(verts) - unique_count
    # 共面面片合并候选（法线相同的相邻面——启发式：共享边+同法线）
    face_normals = [_face_normal(verts, f) for f in faces]
    merge_candidates = 0
    edge_face_map: dict[tuple, list] = defaultdict(list)
    for fi, (a, b, c) in enumerate(faces):
        for e in ((min(a, b), max(a, b)), (min(b, c), max(b, c)),
                  (min(a, c), max(a, c))):
            edge_face_map[e].append(fi)
    for e, fs in edge_face_map.items():
        if len(fs) == 2:
            f1, f2 = fs
            n1, n2 = face_normals[f1], face_normals[f2]
            if n1 and n2 and _dot(n1, n2) > 0.999:
                merge_candidates += 1
    current_poly = len(faces)
    suggested = max(1, int(current_poly * (1 - target_ratio)))
    return {"ok": True, "path": path,
            "vertices_before": len(verts), "vertices_after_weld": unique_count,
            "welded_vertices": welded,
            "faces_before": current_poly,
            "coplanar_merge_candidates": merge_candidates // 2,
            "target_ratio": target_ratio,
            "suggested_face_count": min(current_poly, suggested),
            "advice": (f"welding 可省 {welded} 顶点；共面合并候选 "
                       f"{merge_candidates // 2} 对——目标精简 "
                       f"{int(target_ratio * 100)}%（NURBS 概念：表示效率）")}


def _face_normal(verts, f) -> list | None:
    a, b, c = (verts[f[0]], verts[f[1]], verts[f[2]])
    u = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    v = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    n = (u[1] * v[2] - u[2] * v[1],
         u[2] * v[0] - u[0] * v[2],
         u[0] * v[1] - u[1] * v[0])
    ln = math.sqrt(n[0] * n[0] + n[1] * n[1] + n[2] * n[2])
    if ln == 0:
        return None
    return [n[0] / ln, n[1] / ln, n[2] / ln]


def _dot(a, b) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


# ── ③ mesh_splat：三角面片→可训练参数（Triangle Splatting）─
def mesh_splat(path: str) -> dict:
    """三角面片 → 可训练参数表（顶点/法线/面索引张量结构）。

    Triangle Splatting 概念落地：网格参数化为张量——
    真·梯度优化（不透明三角形直接优化）需 GPU 框架（未来方向）。
    """
    m = load_mesh(path)
    if not m.get("ok"):
        return m
    verts, faces = m["vertices"], m["faces"]
    normals = m.get("normals") or [_face_normal(verts, f) or [0, 0, 1]
                                   for f in faces]
    # 参数张量结构（面片级）
    return {"ok": True, "path": path,
            "params": {
                "vertex_tensor": {"shape": [len(verts), 3],
                                  "dtype": "float32",
                                  "data": verts[:20]},
                "face_index_tensor": {"shape": [len(faces), 3],
                                      "dtype": "int32",
                                      "data": faces[:20]},
                "normal_tensor": {"shape": [len(faces), 3],
                                  "dtype": "float32",
                                  "data": normals[:20]},
            },
            "tensor_summary": {
                "vertices": len(verts), "faces": len(faces),
                "param_count": len(verts) * 3 + len(faces) * 3,
            },
            "advice": "三角面片已参数化为张量（可训练参数基础设施）——"
                      "真·梯度优化需 GPU 渲染框架（Triangle Splatting 未来方向）"}


# ── ④ voxelize：体素表示（Radiant Foam 概念）───────────────
def voxelize(path: str, resolution: int = 16) -> dict:
    """网格体素化：包围盒网格采样 + 三角形相交测试（纯 Python）。

    Radiant Foam 概念落地：体素占用表示（体素光线追踪分析基础）。
    """
    if not 4 <= resolution <= 128:
        return {"ok": False, "error": "resolution 须在 4..128"}
    m = load_mesh(path)
    if not m.get("ok"):
        return m
    verts, faces = m["vertices"], m["faces"]
    # 包围盒
    xs = [v[0] for v in verts]
    ys = [v[1] for v in verts]
    zs = [v[2] for v in verts]
    bmin = (min(xs), min(ys), min(zs))
    bmax = (max(xs), max(ys), max(zs))
    span = max(bmax[0] - bmin[0], bmax[1] - bmin[1],
               bmax[2] - bmin[2], 1e-6)
    cell = span / resolution
    # 网格采样：体素中心 → 三角形包含测试（射线法——向 +X 射线计数）
    occupied = 0
    for xi in range(resolution):
        cx = bmin[0] + (xi + 0.5) * cell
        for yi in range(resolution):
            cy = bmin[1] + (yi + 0.5) * cell
            for zi in range(resolution):
                cz = bmin[2] + (zi + 0.5) * cell
                if _point_in_mesh((cx, cy, cz), verts, faces):
                    occupied += 1
    total = resolution ** 3
    density = occupied / total if total else 0
    return {"ok": True, "path": path, "resolution": resolution,
            "bbox": {"min": bmin, "max": bmax, "span": round(span, 4)},
            "occupied_voxels": occupied, "total_voxels": total,
            "density": round(density, 4),
            "advice": (f"体素占用 {occupied}/{total}（密度 {density:.2%}）——"
                       "Radiant Foam 概念基础：体素表示可做光线追踪/碰撞")}


def _point_in_mesh(p, verts, faces) -> bool:
    """射线法：向 +X 方向发射射线，与三角形相交奇数次 = 在内部。"""
    hits = 0
    for (a, b, c) in faces:
        if _ray_tri_intersect(p, verts[a], verts[b], verts[c]):
            hits += 1
    return hits % 2 == 1


def _ray_tri_intersect(origin, v0, v1, v2) -> bool:
    """Möller–Trumbore 射线-三角形相交（射线：origin 向 +X）。"""
    e1 = (v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2])
    e2 = (v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2])
    dir = (1.0, 0.0, 0.0)
    pvec = _cross(dir, e2)
    det = _dot(e1, pvec)
    if abs(det) < 1e-9:
        return False
    inv = 1.0 / det
    tvec = (origin[0] - v0[0], origin[1] - v0[1], origin[2] - v0[2])
    u = _dot(tvec, pvec) * inv
    if u < 0 or u > 1:
        return False
    qvec = _cross(tvec, e1)
    v = _dot(dir, qvec) * inv
    if v < 0 or u + v > 1:
        return False
    t = _dot(e2, qvec) * inv
    return t > 0  # 正向射线命中


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])
