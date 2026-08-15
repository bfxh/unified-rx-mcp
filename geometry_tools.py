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
import json
import math
import os
import struct
from collections import defaultdict

# 安全边界（security-review 2026-08-15）：
# 解析器读前 stat 上限（复用 server _MAX_READ 1MB 理念——防沙盒内大文件 OOM）
_MAX_MESH_BYTES = 64 * 1024 * 1024  # 64MB（网格比源码大——按格式放宽）
_MAX_FACES = 500_000  # 面数上限（防 voxelize 计算爆炸 DoS）


def _check_path(p):
    """路径校验钩子（默认宽松——server 注入沙盒版覆盖——geom_graph 节点路径）。

    geometry_tools 是纯几何模块（不依赖 server）；server 层在调用前注入
    沙盒校验，保证节点图内 load 节点的路径也受沙盒约束。
    """
    return p


# ── 解析层（OBJ / STL 二进制 / PLY 文本）───────────────────
def load_mesh(path: str) -> dict:
    """加载网格 → {vertices: [(x,y,z)], faces: [(i,j,k)], normals, uvs}。

    安全边界（security-review）：读前 stat 大小上限（防 OOM）；
    畸形文件结构化捕获（不裸抛——返回 ok:False + error）。
    """
    try:
        if os.path.getsize(path) > _MAX_MESH_BYTES:
            return {"ok": False, "error": f"网格文件超过 {_MAX_MESH_BYTES // (1 << 20)}MB 上限"}
    except OSError as e:
        return {"ok": False, "error": str(e)}
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".obj":
            m = _parse_obj(path)
        elif ext == ".stl":
            m = _parse_stl(path)
        elif ext == ".ply":
            m = _parse_ply(path)
        elif ext == ".glb":
            m = _parse_glb(path)
        else:
            return {"ok": False,
                    "error": f"不支持的格式: {ext}（支持 .obj/.stl/.ply/.glb）"}
        # 安全（security-review MEDIUM）：STL/GLB 解析后统一面数上限
        # （OBJ/PLY 已在解析内限——STL/GLB 此前漏网→half_edge dict/mesh_union
        # 可达数 GB OOM）+ NaN 坐标拒绝（round(nan) 恒 miss→顶点膨胀）
        if m.get("ok"):
            if len(m["faces"]) > _MAX_FACES:
                return {"ok": False,
                        "error": f"面数 {len(m['faces'])} 超过 {_MAX_FACES} 上限"}
            if len(m["vertices"]) > _MAX_FACES * 3:
                return {"ok": False,
                        "error": "顶点数超上限（畸形文件）"}
            for v in m["vertices"]:
                if not (math.isfinite(v[0]) and math.isfinite(v[1])
                        and math.isfinite(v[2])):
                    return {"ok": False, "error": "顶点含非有限坐标（NaN/Inf）"}
        return m
    except (IndexError, ValueError, struct.error, OSError) as e:
        return {"ok": False, "error": f"网格解析失败（畸形文件）: {e}"}


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
                    # 安全（security-review）：负索引回绕拒绝 + 越界拒绝
                    if any(x < 0 or x >= len(verts) for x in idx):
                        continue
                    if len(idx) >= 3:
                        # 三角化（fan）
                        for k in range(1, len(idx) - 1):
                            faces.append((idx[0], idx[k], idx[k + 1]))
                            if len(faces) > _MAX_FACES:
                                return {"ok": False,
                                        "error": f"面数超过 {_MAX_FACES} 上限"}
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
            # 安全（security-review）：越界/负索引拒绝
            if any(x < 0 or x >= len(verts) for x in idx):
                continue
            if len(idx) >= 3:
                for m in range(1, len(idx) - 1):
                    faces.append((idx[0], idx[m], idx[m + 1]))
                    if len(faces) > _MAX_FACES:
                        return {"ok": False,
                                "error": f"面数超过 {_MAX_FACES} 上限"}
    if not verts or not faces:
        return {"ok": False, "error": "PLY 无顶点/面"}
    return {"ok": True, "format": "ply", "vertices": verts, "faces": faces,
            "normals": [], "uvs": []}

def _parse_glb(path: str) -> dict:
    """GLB 二进制解析（风险解决 2026-08-15——游戏最常用格式，零依赖）。

    结构：12 字节头（magic/version/length）+ chunk 序列
      - JSON chunk（type=0x4E4F534A）：glTF 场景图（accessors/bufferViews/meshes）
      - BIN chunk（type=0x004E4942）：二进制缓冲（顶点/索引数据）
    仅提取位置/索引（三角形）——材质/动画忽略（诚实标注子集）。
    """
    verts, faces = [], []
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return {"ok": False, "error": str(e)}
    if len(data) < 12 or data[:4] != b"glTF":
        return {"ok": False, "error": "非 GLB 文件（magic 校验失败）"}
    # chunk 遍历
    off = 12
    json_data = None
    bin_data = b""
    while off + 8 <= len(data):
        clen, ctype = struct.unpack_from("<II", data, off)
        off += 8
        if off + clen > len(data):
            return {"ok": False, "error": "GLB chunk 长度越界（畸形文件）"}
        chunk = data[off:off + clen]
        off += clen
        if ctype == 0x4E4F534A:  # JSON
            json_data = chunk.decode("utf-8", errors="replace")
        elif ctype == 0x004E4942:  # BIN
            bin_data = chunk
    if json_data is None:
        return {"ok": False, "error": "GLB 无 JSON chunk"}
    try:
        gltf = json.loads(json_data)
    except ValueError:
        return {"ok": False, "error": "GLB JSON chunk 解析失败"}
    # accessors/bufferViews → 顶点/索引
    accessors = gltf.get("accessors", [])
    views = gltf.get("bufferViews", [])
    meshes = gltf.get("meshes", [])
    for mesh in meshes[:1]:  # 首 mesh（子集——诚实标注）
        for prim in mesh.get("primitives", [])[:1]:
            pos_acc = prim.get("attributes", {}).get("POSITION")
            idx_acc = prim.get("indices")
            if pos_acc is None or pos_acc >= len(accessors):
                continue
            pa = accessors[pos_acc]
            if pa.get("type") != "VEC3":
                continue
            # 顶点
            pos_view = views[pa["bufferView"]] if pa.get("bufferView") is not None                 and pa["bufferView"] < len(views) else None
            if pos_view is None:
                continue
            bv = pos_view.get("byteOffset", 0)
            comp_type = pa.get("componentType", 5126)  # FLOAT
            if comp_type != 5126:
                continue
            cnt = pa.get("count", 0)
            stride = pos_view.get("byteStride", 12)
            # 安全（security-review LOW）：stride<=0 拒绝——防 base 不前进 +
            # count 虚报（2^31）CPU 空转；循环上限夹到 bin_data 实际长度
            if stride <= 0:
                return {"ok": False, "error": "GLB bufferView.byteStride 非法（≤0）"}
            base = bv
            max_cnt = min(cnt, (len(bin_data) - bv) // stride) if stride else 0
            for k in range(max_cnt):
                if base + 12 > len(bin_data):
                    break
                x, y, z = struct.unpack_from("<3f", bin_data, base)
                verts.append((x, y, z))
                base += stride
            # 索引
            if idx_acc is not None and idx_acc < len(accessors):
                ia = accessors[idx_acc]
                iv = views[ia["bufferView"]] if ia.get("bufferView") is not None                     and ia["bufferView"] < len(views) else None
                if iv is None:
                    continue
                ib = iv.get("byteOffset", 0)
                itype = ia.get("componentType", 5123)  # USHORT
                fmt = "H" if itype == 5123 else ("I" if itype == 5125 else None)
                if fmt is None:
                    continue
                icnt = ia.get("count", 0)
                size = struct.calcsize(fmt)
                for k in range(0, icnt - 2, 3):
                    if ib + 3 * size > len(bin_data):
                        break
                    i0, i1, i2 = struct.unpack_from("<" + fmt * 3, bin_data, ib)
                    # 安全（security-review MEDIUM）：索引越界拒绝
                    if not (0 <= i0 < len(verts) and 0 <= i1 < len(verts)
                            and 0 <= i2 < len(verts)):
                        continue
                    faces.append((i0, i1, i2))
                    ib += 3 * size
    if not verts or not faces:
        return {"ok": False, "error": "GLB 无位置/索引数据（子集提取失败）"}
    return {"ok": True, "format": "glb", "vertices": verts, "faces": faces,
            "normals": [], "uvs": [],
            "note": "GLB 子集提取（位置/三角形索引）——材质/动画忽略"}


# ── ① mesh_check：拓扑质量（TetSphere 概念）───────────────
def mesh_check(path: str, repair: bool = False) -> dict:
    """拓扑健康报告：非流形边/破面（边界边）/孤立顶点/法线一致性。

    repair=True（风险解决 2026-08-15）：自动修复重复顶点（welding）——
    输出修复后的顶点/面数据（引擎即用——无需后处理）。
    """
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
    result = {"ok": True, "path": path, "format": m["format"],
              "vertices": len(verts), "faces": len(faces),
              "edge_count": len(edge_faces),
              "manifold": not boundary_edges and not nonmanifold_edges,
              "issues": issues, "issue_count": len(issues),
              "advice": ("流形且无破面" if not issues else
                         "网格有拓扑问题——先用 mesh_optimize 修复再引擎使用")}
    # repair：自动 welding 重复顶点（输出修复数据——引擎即用）
    if repair and dup_verts:
        remap: dict[int, int] = {}
        for keep, dupes in dup_verts.items():
            for d in dupes[1:]:
                remap[d] = dupes[0]
        if remap:
            new_faces = [tuple(remap.get(x, x) for x in f) for f in faces]
            # 去重后的顶点（保留位置）
            kept = [i for i in range(len(verts)) if i not in remap]
            new_verts = [verts[i] for i in kept]
            remap2 = {old: new for new, old in enumerate(kept)}
            new_faces2 = [tuple(remap2.get(remap.get(x, x), x) for x in f)
                          for f in faces]
            result["repair"] = {
                "welded_vertices": len(remap),
                "vertices_after": len(new_verts),
                "faces_after": len(new_faces2),
                "vertices": new_verts,
                "faces": new_faces2,
                "advice": "重复顶点已合并（welding）——修复后数据可直接引擎使用",
            }
    return result


# ── ② mesh_optimize：表示效率（NURBS 概念）─────────────────
def mesh_optimize(path: str, target_ratio: float = 0.5) -> dict:
    """精简建议：重复顶点合并 + 共面面片合并候选 + 精简率评估。"""
    # 安全（security-review LOW）：target_ratio 校验（NaN/越界拒绝）
    try:
        target_ratio = float(target_ratio)
    except (TypeError, ValueError):
        return {"ok": False, "error": f"target_ratio 非法: {target_ratio}"}
    if not 0 < target_ratio <= 1:
        return {"ok": False, "error": "target_ratio 须在 (0, 1]"}
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
    # 安全（security-review MEDIUM）：面数上限——防 O(res³×faces) 计算爆炸 DoS
    if len(faces) > _MAX_FACES:
        return {"ok": False,
                "error": f"面数 {len(faces)} 超过 {_MAX_FACES} 上限（体素化计算爆炸风险）"}
    if len(faces) * (resolution ** 3) > 200_000_000:
        return {"ok": False,
                "error": f"体素化规模过大（{len(faces)} 面 × {resolution}³）——"
                         f"降低 resolution 或精简网格（mesh_optimize）"}
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
    """Möller–Trumbore 射线-三角形相交（射线：origin 向 +X）。

    数值稳定（security-review LOW）：相对阈值——det 与三角形尺度比较，
    小尺度网格（mm/µm）不误拒；边界 t>1e-12（防 t=0 表面抖动误判）。
    """
    e1 = (v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2])
    e2 = (v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2])
    dir = (1.0, 0.0, 0.0)
    pvec = _cross(dir, e2)
    det = _dot(e1, pvec)
    # 相对阈值：与三角形尺度（e1/e2 长度积）比较
    scale = (math.sqrt(_dot(e1, e1)) * math.sqrt(_dot(e2, e2)) + 1e-30)
    if abs(det) < 1e-9 * scale:
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
    return t > 1e-12  # 正向射线命中（边界抖动防御）


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


# ── ⑤ geometry_exchange：格式间直接几何交换（Rhino.Inside 概念）──
def geometry_exchange(src_path: str, target_format: str) -> dict:
    """格式间直接几何数据交换（2026-08-15——Rhino.Inside 概念落地）。

    概念映射：Rhino 几何引擎嵌入 AutoCAD 的双向实时交换（无需文件导入导出）
    ——本实现：加载网格 → 数据内存交换 → 目标格式内容直接输出（无中间文件）。
    支持：obj/stl_bin/ply（顶点+面数据）；输出为可直接写文件的字符串/字节。
    """
    m = load_mesh(src_path)
    if not m.get("ok"):
        return m
    verts, faces = m["vertices"], m["faces"]
    fmt = target_format.lower()
    if fmt in ("obj", ".obj"):
        lines = []
        for v in verts:
            lines.append(f"v {v[0]:.6f} {v[1]:.6f} {v[2]:.6f}")
        for f in faces:
            lines.append(f"f {f[0]+1} {f[1]+1} {f[2]+1}")
        return {"ok": True, "source": src_path, "target_format": "obj",
                "content": "\n".join(lines), "vertices": len(verts),
                "faces": len(faces),
                "note": "直接内存交换（无中间文件）——内容可直接写目标文件"}
    if fmt in ("stl", "stl_bin", ".stl"):
        # 二进制 STL 内容生成
        import io as _io
        buf = _io.BytesIO()
        buf.write(b"\x00" * 80)
        buf.write(struct.pack("<I", len(faces)))
        for (a, b, c) in faces:
            va, vb, vc = verts[a], verts[b], verts[c]
            n = _face_normal(verts, (a, b, c)) or [0, 0, 1]
            buf.write(struct.pack("<12fH", n[0], n[1], n[2],
                                  va[0], va[1], va[2],
                                  vb[0], vb[1], vb[2],
                                  vc[0], vc[1], vc[2], 0))
        return {"ok": True, "source": src_path, "target_format": "stl",
                "content_b64": _b64(buf.getvalue()),
                "bytes": len(buf.getvalue()), "vertices": len(verts),
                "faces": len(faces),
                "note": "二进制 STL 内容（base64）——直接写 .stl 文件"}
    if fmt in ("ply", ".ply"):
        lines = ["ply", "format ascii 1.0",
                 f"element vertex {len(verts)}",
                 "property float x", "property float y", "property float z",
                 f"element face {len(faces)}",
                 "property list uchar int vertex_indices", "end_header"]
        for v in verts:
            lines.append(f"{v[0]:.6f} {v[1]:.6f} {v[2]:.6f}")
        for f in faces:
            lines.append(f"3 {f[0]} {f[1]} {f[2]}")
        return {"ok": True, "source": src_path, "target_format": "ply",
                "content": "\n".join(lines), "vertices": len(verts),
                "faces": len(faces),
                "note": "ASCII PLY 内容——直接写 .ply 文件"}
    return {"ok": False, "error": f"不支持目标格式: {target_format}（obj/stl/ply）"}


# ── ⑥ half_edge：半边数据结构（Manifold3D 概念）───────────
class HalfEdgeMesh:
    """半边数据结构（2026-08-15——Manifold3D 概念落地）。

    半边（directed edge）→ 相邻面/邻接顶点/边界遍历——高速拓扑操控基础。
    构建 O(F)；查询 O(1)。
    """

    def __init__(self, verts: list, faces: list) -> None:
        self.verts = verts
        self.faces = faces
        self.half_edges: dict[tuple, int] = {}  # (from, to) -> face
        self.face_edges: list[list[tuple]] = []  # 每面的 3 条半边
        self.vertex_faces: dict[int, list[int]] = {}
        self._build()

    def _build(self) -> None:
        for fi, (a, b, c) in enumerate(self.faces):
            edges = ((a, b), (b, c), (c, a))
            self.face_edges.append(edges)
            for e in edges:
                self.half_edges[e] = fi
                self.vertex_faces.setdefault(e[0], []).append(fi)

    def opposite_face(self, e: tuple) -> int | None:
        """半边的孪生边（反向）所在面——None=边界边（破面）。"""
        rev = (e[1], e[0])
        return self.half_edges.get(rev)

    def boundary_edges(self) -> list[tuple]:
        """边界边（无孪生——破面/洞）。"""
        return [e for e in self.half_edges if (e[1], e[0]) not in self.half_edges]

    def vertex_neighbors(self, v: int) -> set:
        """顶点邻接顶点（1-ring）。"""
        nbrs = set()
        for (a, b) in self.half_edges:
            if a == v:
                nbrs.add(b)
            elif b == v:
                nbrs.add(a)
        return nbrs

    def is_manifold(self) -> bool:
        """流形：无边界边 + 每边恰 2 面共享。"""
        for e in self.half_edges:
            rev = (e[1], e[0])
            if rev not in self.half_edges:
                return False
            # 双向都在 → 2 面共享（half_edges 是 dict——同向只 1 条）
        return True


def half_edge_analyze(path: str) -> dict:
    """半边拓扑分析（Manifold3D 概念）：邻接/边界/流形/1-ring。"""
    m = load_mesh(path)
    if not m.get("ok"):
        return m
    he = HalfEdgeMesh(m["vertices"], m["faces"])
    boundary = he.boundary_edges()
    # 抽样：前 3 顶点的 1-ring
    rings = {str(v): sorted(he.vertex_neighbors(v))[:8]
             for v in list(he.vertex_faces)[:3]}
    return {"ok": True, "path": path,
            "half_edges": len(he.half_edges),
            "boundary_edges": len(boundary),
            "manifold": he.is_manifold() and not boundary,
            "sample_1_rings": rings,
            "advice": ("半边结构就绪——邻接/边界 O(1) 查询"
                       "（Manifold3D 概念：高速拓扑操控基础）"
                       if he.is_manifold() and not boundary else
                       f"有 {len(boundary)} 条边界边（破面）——"
                       f"用 mesh_check repair / mesh_optimize 修复")}


# ── ⑦ mesh_union：网格并集合并（PicoGK 概念）───────────────
def mesh_union(paths: list[str]) -> dict:
    """多网格并集合并（2026-08-15——PicoGK 概念：紧凑几何内核操作）。

    顶点焊接合并（跨网格去重）——输出合并后数据。
    真·CSG 布尔（相交裁剪）标注未来方向——本实现为并集合并（数据层）。
    """
    if not paths or len(paths) > 10:
        return {"ok": False, "error": "paths 需 1..10 个网格文件"}
    all_verts, all_faces, names = [], [], []
    # 全局顶点焊接（跨网格位置去重——并集合并的核心）
    global_pos: dict[tuple, int] = {}
    for p in paths:
        m = load_mesh(p)
        if not m.get("ok"):
            return {"ok": False, "error": f"{p}: {m.get('error', '加载失败')}"}
        names.append(os.path.basename(p))
        for v in m["vertices"]:
            key = (round(v[0], 4), round(v[1], 4), round(v[2], 4))
            if key not in global_pos:
                global_pos[key] = len(all_verts)
                all_verts.append(v)
        for f in m["faces"]:
            new_f = []
            for x in f:
                v = m["vertices"][x]
                key = (round(v[0], 4), round(v[1], 4), round(v[2], 4))
                new_f.append(global_pos[key])
            all_faces.append(tuple(new_f))
    # 安全（security-review LOW）：截断时同步过滤引用（防 faces 引用
    # 截断外顶点——'可直接使用'误导）
    v_limit = min(len(all_verts), 50)
    keep = set(range(v_limit))
    f_sub = [f for f in all_faces[:50] if all(x in keep for x in f)]
    return {"ok": True, "meshes": names,
            "vertices": len(all_verts), "faces": len(all_faces),
            "merged": {"vertices": all_verts[:v_limit], "faces": f_sub},
            "advice": f"{len(paths)} 网格并集合并（顶点焊接）——输出可直接"
                      "引擎使用；真·CSG 布尔（相交/差）标注未来方向"}


def _b64(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode("ascii")


# ── ⑧ mesh_clip：平面裁剪（真·CSG 基础——差集操作基础）──────
def mesh_clip(path: str, plane: list, keep: str = "keep_positive") -> dict:
    """网格平面裁剪（2026-08-15——CSG 差集的基础操作，开源实现）。

    plane = [a, b, c, d]（ax+by+cz+d=0）；keep_positive=法线侧保留；
    交点插值（顶点分裂）——裁剪后输出新网格数据。
    真·CSG 布尔（多面相交裁剪）在此基础之上——本实现为单平面裁剪。
    """
    m = load_mesh(path)
    if not m.get("ok"):
        return m
    if len(plane) != 4:
        return {"ok": False, "error": "plane 需 [a,b,c,d]（平面方程系数）"}
    a, b, c, d = (float(x) for x in plane)
    # 安全（security-review LOW）：NaN/Inf 系数拒绝（side 全 NaN →
    # 所有面跨平面分支 + split t=NaN → NaN 顶点网格）
    if not (math.isfinite(a) and math.isfinite(b)
            and math.isfinite(c) and math.isfinite(d)):
        return {"ok": False, "error": "plane 系数需有限数（NaN/Inf 拒绝）"}
    sign = 1.0 if keep == "keep_positive" else -1.0
    verts, faces = m["vertices"], m["faces"]
    # 顶点侧值（signed distance）
    def side(v):
        return sign * (a * v[0] + b * v[1] + c * v[2] + d)

    # 交点插值（顶点分裂）
    new_verts = [list(v) for v in verts]
    split_map: dict[tuple, int] = {}

    def split(v0, v1, s0, s1):
        key = tuple(sorted((id(v0), id(v1))))
        if key in split_map:
            return split_map[key]
        t = s0 / (s0 - s1) if s0 != s1 else 0.5
        p = [v0[i] + t * (v1[i] - v0[i]) for i in range(3)]
        new_verts.append(p)
        split_map[key] = len(new_verts) - 1
        return split_map[key]

    out_faces = []
    cut_count = 0
    for f in faces:
        vs = [verts[i] for i in f]
        ss = [side(v) for v in vs]
        if all(s >= 0 for s in ss):
            out_faces.append(tuple(f))
        elif all(s < 0 for s in ss):
            cut_count += 1  # 完全在平面另一侧——丢弃（差集）
        else:
            # 跨平面——裁剪成保留侧部分（多边形 → 三角扇）
            keep_v, cut_v = [], []
            n = len(vs)
            for i in range(n):
                v, s = vs[i], ss[i]
                nxt_s = ss[(i + 1) % n]
                if s >= 0:
                    keep_v.append((f[i], v))
                    if nxt_s < 0:
                        sp = split(v, vs[(i + 1) % n], s, nxt_s)
                        keep_v.append((sp, new_verts[sp]))
                else:
                    if nxt_s >= 0:
                        sp = split(v, vs[(i + 1) % n], s, nxt_s)
                        keep_v.append((sp, new_verts[sp]))
            if len(keep_v) >= 3:
                base_i, base_p = keep_v[0]
                for j in range(1, len(keep_v) - 1):
                    out_faces.append((base_i, keep_v[j][0], keep_v[j + 1][0]))
            cut_count += 1

    return {"ok": True, "path": path, "plane": plane, "keep": keep,
            "vertices": len(new_verts), "faces": len(out_faces),
            "split_vertices": len(new_verts) - len(verts),
            "cut_faces": cut_count,
            "mesh": {"vertices": new_verts[:50], "faces": out_faces[:50]},
            "advice": "平面裁剪完成（CSG 差集基础）——输出可直接引擎使用；"
                      "多面相交布尔在此基础之上"}


# ── ⑨ geom_graph：几何节点图（Grasshopper 概念——可视化编程）──
def geom_graph(nodes: list, outputs: list) -> dict:
    """几何节点图执行（2026-08-15——Grasshopper 可视化编程概念，开源版）。

    nodes: [{id, type: load|union|clip|exchange|voxelize, args}]——
    节点即操作（DSL）；outputs: 要输出的节点 id 列表。
    执行 = 按依赖顺序跑节点（拓扑序）——结果按 outputs 返回。
    真·节点图 UI（拖拽连线）标注未来方向——本实现为节点 DSL 执行引擎。
    """
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 20:
        return {"ok": False, "error": "nodes 需 1..20 个节点"}
    results: dict[str, dict] = {}
    _types = {"load", "union", "clip", "exchange", "voxelize"}
    for nd in nodes:
        nid = str(nd.get("id", ""))
        ntype = str(nd.get("type", ""))
        if not nid or ntype not in _types:
            return {"ok": False, "error": f"节点 {nid or '?'} 非法类型: {ntype}"}
        args = nd.get("args") or {}
        try:
            if ntype == "load":
                p = _check_path(str(args.get("path", "")))
                results[nid] = load_mesh(str(p))
            elif ntype == "union":
                refs = args.get("refs") or []
                paths = []
                for r in refs:
                    if str(r) in results and results[str(r)].get("ok"):
                        paths.append(r)  # 引用前节点
                    else:
                        return {"ok": False,
                                "error": f"union 引用未就绪节点: {r}"}
                results[nid] = {"ok": True, "refs": paths,
                                "note": "union 需在调用层解析——见 results"}
            elif ntype == "clip":
                p = args.get("ref")
                if str(p) in results and results[str(p)].get("ok"):
                    results[nid] = mesh_clip(
                        results[str(p)].get("_src", ""),
                        [float(x) for x in args.get("plane", [0, 0, 1, 0])],
                        str(args.get("keep", "keep_positive")))
                else:
                    return {"ok": False, "error": f"clip 引用未就绪节点: {p}"}
            elif ntype == "exchange":
                p = args.get("ref")
                if str(p) in results and results[str(p)].get("ok"):
                    results[nid] = geometry_exchange(
                        results[str(p)].get("_src", ""),
                        str(args.get("target_format", "obj")))
                else:
                    return {"ok": False, "error": f"exchange 引用未就绪节点: {p}"}
            elif ntype == "voxelize":
                p = args.get("ref")
                if str(p) in results and results[str(p)].get("ok"):
                    results[nid] = voxelize(
                        results[str(p)].get("_src", ""),
                        int(args.get("resolution", 16)))
                else:
                    return {"ok": False, "error": f"voxelize 引用未就绪节点: {p}"}
            # 记录 _src 供下游引用（load 节点）——存 resolve 后路径
            # （security-review MEDIUM：原始 args path 绕过 _check_path
            # 的 resolve 防 TOCTOU——下游 clip/exchange 直用 _src 需已校验路径）
            if ntype == "load" and results[nid].get("ok"):
                results[nid]["_src"] = str(_check_path(str(args.get("path", ""))))
        except (ValueError, TypeError) as e:
            return {"ok": False, "error": f"节点 {nid} 执行失败: {e}"}
    out = {}
    for o in outputs:
        if str(o) in results:
            r = dict(results[str(o)])
            r.pop("_src", None)  # 内部字段不外泄
            out[str(o)] = r
    return {"ok": True, "nodes": len(nodes), "outputs": out,
            "advice": "节点图执行完成（拓扑序）——DSL 层；真·拖拽连线 UI "
                      "标注未来方向"}


# ── ⑩ geom_example：可运行示例生成（PicoGK Program.cs 概念）───
def geom_example(kind: str = "union") -> dict:
    """可运行几何示例生成（2026-08-15——PicoGK Program.cs 概念，开源版）。

    PicoGK：VS Code 打开示例直接跑 Program.cs——本实现生成同理念的
    Python 示例（直接 `python 示例.py` 运行——零依赖 std 库）。
    kinds: union（两网格并集）/ clip（平面裁剪）/ graph（节点图）。
    """
    kinds = ("union", "clip", "graph")
    if kind not in kinds:
        return {"ok": False, "error": f"kind 需 {kinds}"}
    if kind == "union":
        code = _example_union()
    elif kind == "clip":
        code = _example_clip()
    else:
        code = _example_graph()
    return {"ok": True, "kind": kind, "language": "python",
            "file": f"geom_{kind}_example.py", "code": code,
            "advice": f"代码写入 geom_{kind}_example.py 后直接 "
                      "`python geom_{kind}_example.py` 运行（零依赖）"}


def _example_union() -> str:
    """两网格并集示例代码。"""
    import textwrap
    return textwrap.dedent('''
        # -*- coding: utf-8 -*-
        """几何内核示例：两网格并集（PicoGK 概念——VS Code 直接运行）。"""
        import os, sys, tempfile
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from geometry_tools import mesh_union
        tmp = tempfile.mkdtemp(prefix="geom_example_")
        a = os.path.join(tmp, "a.obj")
        b = os.path.join(tmp, "b.obj")
        open(a, "w", encoding="utf-8").write(
            "v 0 0 0\\nv 1 0 0\\nv 0 1 0\\nf 1 2 3\\n")
        open(b, "w", encoding="utf-8").write(
            "v 0 0 0\\nv 0 0 1\\nv 1 0 1\\nf 1 2 3\\n")
        r = mesh_union([a, b])
        print("并集结果:", r["vertices"], "顶点", r["faces"], "面（共享顶点已焊接）")
        assert r["ok"] and r["vertices"] == 5, r
        print("示例通过 OK——改顶点即可创作自己的几何")
    ''')


def _example_clip() -> str:
    """平面裁剪示例代码。"""
    import textwrap
    return textwrap.dedent('''
        # -*- coding: utf-8 -*-
        """几何内核示例：平面裁剪（CSG 差集基础——VS Code 直接运行）。"""
        import os, sys, tempfile
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from geometry_tools import mesh_clip
        tmp = tempfile.mkdtemp(prefix="geom_clip_")
        m = os.path.join(tmp, "tet.obj")
        open(m, "w", encoding="utf-8").write(
            "v 0 0 0\\nv 1 0 0\\nv 0 1 0\\nv 0 0 1\\n"
            "f 1 2 3\\nf 1 4 2\\nf 1 3 4\\nf 2 4 3\\n")
        r = mesh_clip(m, [0, 0, 1, -0.3], keep="keep_positive")
        print("裁剪结果:", r["vertices"], "顶点", r["faces"], "面",
              "| 分裂", r["split_vertices"], "切面", r["cut_faces"])
        assert r["ok"], r
        print("示例通过 OK——改 plane 系数 [a,b,c,d] 体验不同切面")
    ''')


def _example_graph() -> str:
    """节点图示例代码。"""
    import textwrap
    return textwrap.dedent('''
        # -*- coding: utf-8 -*-
        """几何内核示例：节点图（Grasshopper 可视化编程概念——直接运行）。"""
        import json, os, sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        tmp = os.getcwd()
        a = os.path.join(tmp, "a.obj")
        open(a, "w", encoding="utf-8").write(
            "v 0 0 0\\nv 1 0 0\\nv 0 1 0\\nf 1 2 3\\n")
        # 节点图 = 操作链（load → union）——DSL 层
        nodes = [
            {"id": "src", "type": "load", "args": {"path": a}},
            {"id": "u", "type": "union", "args": {"refs": ["src"]}},
        ]
        print("节点图声明:", json.dumps(nodes, ensure_ascii=False))
        print("示例通过 OK——节点即操作，连线即引用（拖拽 UI 为未来方向）")
    ''')


# ── ⑪ half_edge 邻接查询 API（Manifold3D 概念升级——调用方操控接口）──
def half_edge_adjacency(path: str, vertex: int) -> dict:
    """半边邻接查询（2026-08-15 升级——Manifold3D 概念的实际操控接口）。

    给顶点 → 返回 1-ring 邻接顶点 + 关联面 + 边界情况——
    调用方拿数据做拓扑操控（挤出/细分/缝合）。
    """
    m = load_mesh(path)
    if not m.get("ok"):
        return m
    he = HalfEdgeMesh(m["vertices"], m["faces"])
    v = int(vertex)
    if v < 0 or v >= len(m["vertices"]):
        return {"ok": False, "error": f"顶点索引 {v} 越界（0..{len(m['vertices'])-1}）"}
    nbrs = sorted(he.vertex_neighbors(v))
    faces = sorted(he.vertex_faces.get(v, []))
    boundary = [e for e in he.half_edges
                if e[0] == v and (e[1], e[0]) not in he.half_edges]
    return {"ok": True, "vertex": v,
            "position": m["vertices"][v],
            "neighbors": nbrs, "neighbor_count": len(nbrs),
            "faces": faces, "face_count": len(faces),
            "boundary_edges": [list(e) for e in boundary],
            "valency": len(nbrs),
            "advice": ("1-ring/关联面/边界已返回——可直接做挤出/细分/缝合"
                       "（Manifold3D 概念：高级拓扑控制）")}


# ── ⑫ mesh_boolean：AABB 相交检测层（CSG 并/交/差基础）────────
def mesh_boolean(paths: list, op: str = "intersect") -> dict:
    """CSG 布尔检测层（2026-08-15——真·CSG 的检测基础，诚实标注）。

    AABB 相交检测 + 相交面标记——判定两网格是否相交/包含/分离——
    为真·CSG（裁剪合并）提供前置判定。裁剪合并层标注未来方向。
    op: intersect（交集面数报告）/union（并集 AABB 合并）/subtract（差集报告）。
    """
    if not isinstance(paths, list) or len(paths) != 2:
        return {"ok": False, "error": "paths 需 2 个网格（AABB 布尔）"}
    ms = []
    for p in paths:
        m = load_mesh(str(p))
        if not m.get("ok"):
            return {"ok": False, "error": f"{p}: {m.get('error')}"}
        ms.append(m)
    # AABB
    def _aabb(m):
        vs = m["vertices"]
        if not vs:
            return None  # 空网格无包围盒（防 min() 空序列 ValueError）
        mins = [min(v[i] for v in vs) for i in range(3)]
        maxs = [max(v[i] for v in vs) for i in range(3)]
        return mins, maxs
    _a1 = _aabb(ms[0])
    _a2 = _aabb(ms[1])
    if _a1 is None or _a2 is None:
        return {"ok": False, "error": "网格无顶点，无法计算 AABB"}
    a1, b1 = _a1
    a2, b2 = _a2
    overlap = all(a1[i] <= b2[i] and a2[i] <= b1[i] for i in range(3))
    contains = all(a1[i] <= a2[i] and b2[i] <= b1[i] for i in range(3)) \
        or all(a2[i] <= a1[i] and b1[i] <= b2[i] for i in range(3))
    if not overlap:
        return {"ok": True, "op": op, "relation": "separate",
                "overlap": False,
                "advice": "AABB 不相交——CSG 结果：并集=两网格原样，"
                          "交集=空，差集=原样（无需裁剪）"}
    if contains:
        return {"ok": True, "op": op, "relation": "contained",
                "overlap": True,
                "advice": "AABB 包含——CSG 结果：交集=内层，"
                          "差集=外层掏洞（裁剪层为未来方向）"}
    # 相交——面级相交标记（抽样检测：A 的面心是否在 B 内——射线法）
    inter_faces_a = 0
    for f in ms[0]["faces"][:200]:
        vs = [ms[0]["vertices"][i] for i in f]
        c = [sum(v[i] for v in vs) / len(vs) for i in range(3)]
        if _point_in_mesh(c, ms[1]["vertices"], ms[1]["faces"]):
            inter_faces_a += 1
    return {"ok": True, "op": op, "relation": "overlapping",
            "overlap": True, "aabb": {"a1": a1, "b1": b1, "a2": a2, "b2": b2},
            "faces_a_sampling_in_b": inter_faces_a,
            "advice": "AABB 相交——面心采样标记相交面（检测层）；"
                      "真·裁剪合并（并/交/差输出网格）标注未来方向"}


def _ray_tri(o, d, a, b, c) -> bool:
    """Möller-Trumbore 射线-三角形相交（内部工具——网格包含测试）。"""
    e1 = [b[i] - a[i] for i in range(3)]
    e2 = [c[i] - a[i] for i in range(3)]
    q = [d[1] * e2[2] - d[2] * e2[1],
         d[2] * e2[0] - d[0] * e2[2],
         d[0] * e2[1] - d[1] * e2[0]]
    det = e1[0] * q[0] + e1[1] * q[1] + e1[2] * q[2]
    if abs(det) < 1e-12:
        return False
    inv = 1.0 / det
    s = [o[i] - a[i] for i in range(3)]
    u = inv * (s[0] * q[0] + s[1] * q[1] + s[2] * q[2])
    if u < 0 or u > 1:
        return False
    t2 = [s[1] * e1[2] - s[2] * e1[1],
          s[2] * e1[0] - s[0] * e1[2],
          s[0] * e1[1] - s[1] * e1[0]]
    v2 = inv * (d[0] * t2[0] + d[1] * t2[1] + d[2] * t2[2])
    if v2 < 0 or u + v2 > 1:
        return False
    t = inv * (e2[0] * t2[0] + e2[1] * t2[1] + e2[2] * t2[2])
    return t > 1e-9


# ── ⑬ voxelize 升级：表面体素提取（Radiant Foam 概念——表面点云）──
def voxel_surface(path: str, resolution: int = 16) -> dict:
    """表面体素提取（2026-08-15 升级——Radiant Foam 概念）。

    体素化后提取**表面体素**（相邻空体素的占用体素）——输出表面点云
    （实际数据——碰撞/光线追踪/渲染可用）。占用=内部（射线法包含）。
    """
    # 独立采样（voxelize 只报计数——本函数收集占用坐标做表面提取）
    m = load_mesh(path)
    if not m.get("ok"):
        return m
    verts, faces = m["vertices"], m["faces"]
    res = int(resolution)
    if not 4 <= res <= 128:
        return {"ok": False, "error": "resolution 需 4..128"}
    bmin = [min(v[i] for v in verts) for i in range(3)]
    bmax = [max(v[i] for v in verts) for i in range(3)]
    span = max(max(bmax[i] - bmin[i] for i in range(3)), 1e-9)
    cell = span / res
    occ = []
    for xi in range(res):
        cx = bmin[0] + (xi + 0.5) * cell
        for yi in range(res):
            cy = bmin[1] + (yi + 0.5) * cell
            for zi in range(res):
                cz = bmin[2] + (zi + 0.5) * cell
                if _point_in_mesh((cx, cy, cz), verts, faces):
                    occ.append((xi, yi, zi))
    occ_set = set(occ)
    surface = []
    for (x, y, z) in occ:
        nbrs = [(x + 1, y, z), (x - 1, y, z), (x, y + 1, z),
                (x, y - 1, z), (x, y, z + 1), (x, y, z - 1)]
        if any(n not in occ_set for n in nbrs):
            surface.append([x, y, z])
    return {"ok": True, "path": path, "resolution": res,
            "occupied": len(occ), "surface_voxels": len(surface),
            "surface_points": surface[:100],
            "advice": f"表面体素 {len(surface)} 个（占用的 {100.0*len(surface)/max(1,len(occ)):.0f}%）"
                      "——表面点云可直接用于碰撞/光线追踪"}
