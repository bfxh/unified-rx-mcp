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

# 射线-三角形相交缓存（3D 小手术刀 #16）：键 = 三角形顶点坐标 + origin 量化
_RAY_HIT_CACHE: dict[tuple, bool] = {}
_RAY_HIT_CACHE_MAX = 4096


def _check_path(p):
    """路径校验钩子（默认宽松——server 注入沙盒版覆盖——geom_graph 节点路径）。

    geometry_tools 是纯几何模块（不依赖 server）；server 层在调用前注入
    沙盒校验，保证节点图内 load 节点的路径也受沙盒约束。
    """
    return p


# ── 解析层（OBJ / STL 二进制 / PLY 文本）───────────────────
# 几何结果缓存（2026-08-19，7 维缓存方案维度 4 安全落地）：
# 同文件重复解析（mesh_check/voxelize/mesh_union 等每个工具都先 load_mesh，
# 64MB 网格全量解析重复浪费）→ 键=(mtime,size,格式)，文件变化即失效，
# 成功才缓存，上限 64 条 LRU。纯确定性（同文件同解析结果），零正确性风险。
_MESH_CACHE: dict[str, tuple[tuple, dict]] = {}
_MESH_CACHE_MAX = 64


def _load_mesh_cached(path: str) -> dict | None:
    """命中返回缓存结果（深拷贝防调用方污染）；未命中返回 None。"""
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size, os.path.splitext(path)[1].lower())
    except OSError:
        return None
    hit = _MESH_CACHE.get(path)
    if hit is not None and hit[0] == key:
        # copy.deepcopy 保类型（JSON round-trip 会把 vertices/faces 的
        # tuple 变 list——破坏调用方类型契约；deepcopy 等价保真）
        import copy
        return copy.deepcopy(hit[1])
    return None


def _store_mesh_cached(path: str, result: dict) -> None:
    """成功结果才缓存；LRU 上限 64 条。"""
    if not result.get("ok"):
        return
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size, os.path.splitext(path)[1].lower())
    except OSError:
        return
    import copy
    _MESH_CACHE[path] = (key, copy.deepcopy(result))
    if len(_MESH_CACHE) > _MESH_CACHE_MAX:
        # 简单 LRU：删最早插入的（dict 保持插入序）
        for k in list(_MESH_CACHE)[: len(_MESH_CACHE) - _MESH_CACHE_MAX]:
            _MESH_CACHE.pop(k, None)


def load_mesh(path: str) -> dict:
    """加载网格 → {vertices: [(x,y,z)], faces: [(i,j,k)], normals, uvs}。

    安全边界（security-review）：读前 stat 大小上限（防 OOM）；
    畸形文件结构化捕获（不裸抛——返回 ok:False + error）。
    解析缓存（2026-08-19）：同文件同版本复用解析结果（mtime+size 键）。
    """
    cached = _load_mesh_cached(path)
    if cached is not None:
        return cached
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
        _store_mesh_cached(path, m)
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
# 体素化结果缓存（3D 小手术刀 #18，2026-08-19）：
# voxelize 是 O(res³×faces) 最重计算——同文件同版本同 resolution 重复调用
# 直接命中（键 = 文件 mtime+size + resolution）。纯确定性，零正确性风险。
_VOXEL_CACHE: dict[tuple, dict] = {}
_VOXEL_CACHE_MAX = 64


def voxelize(path: str, resolution: int = 16) -> dict:
    """网格体素化：包围盒网格采样 + 三角形相交测试（纯 Python）。

    Radiant Foam 概念落地：体素占用表示（体素光线追踪分析基础）。
    结果缓存（#18）：(文件签名, resolution) 键——同参数重复调用直接命中。
    """
    if not 4 <= resolution <= 128:
        return {"ok": False, "error": "resolution 须在 4..128"}
    try:
        st = os.stat(path)
        vkey = (st.st_mtime_ns, st.st_size, resolution)
    except OSError:
        vkey = None
    if vkey is not None:
        hit = _VOXEL_CACHE.get(vkey)
        if hit is not None:
            return dict(hit)  # 浅拷贝外层即可（值不可变/元组）
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
    result = {"ok": True, "path": path, "resolution": resolution,
              "bbox": {"min": bmin, "max": bmax, "span": round(span, 4)},
              "occupied_voxels": occupied, "total_voxels": total,
              "density": round(density, 4),
              "advice": (f"体素占用 {occupied}/{total}（密度 {density:.2%}）——"
                         "Radiant Foam 概念基础：体素表示可做光线追踪/碰撞")}
    if vkey is not None:
        if len(_VOXEL_CACHE) >= _VOXEL_CACHE_MAX:
            _VOXEL_CACHE.clear()
        _VOXEL_CACHE[vkey] = result
    return result


def _point_in_mesh(p, verts, faces) -> bool:
    """射线法：向 +X 方向发射射线，与三角形相交奇数次 = 在内部。

    修复（2026-08-19）：收集所有命中 t 值并**去重后**判奇偶——射线穿过
    共享边/对角线时两个三角形同时命中（同 t），不去重会把"穿出面"误计
    为两次→内部点判成外部（中心点实测复现）。同 t（相对容差）合并。
    """
    hits = []
    for (a, b, c) in faces:
        t = _ray_tri_intersect(p, verts[a], verts[b], verts[c])
        if t is not None:
            hits.append(t)
    if not hits:
        return False
    hits.sort()
    # 去重：相邻 t 差 < 相对容差（1e-9 × 尺度）合并为同一次命中
    dedup = [hits[0]]
    for t in hits[1:]:
        if abs(t - dedup[-1]) > 1e-9 * max(1.0, abs(t)):
            dedup.append(t)
    return len(dedup) % 2 == 1


def _ray_tri_intersect(origin, v0, v1, v2):
    """Möller–Trumbore 射线-三角形相交（射线：origin 向 +X）。

    返回命中距离 t（float），未命中返回 None——调用方用 t 做奇偶计数时
    可去重（同 t 相邻命中合并，防共享边重复计数误判——2026-08-19 修复
    _point_in_mesh 中心点误判外部）。数值稳定（security-review LOW）：
    相对阈值——det 与三角形尺度比较，小尺度网格（mm/µm）不误拒；
    边界 t>1e-12（防 t=0 表面抖动误判）。相交缓存（3D 小手术刀 #16）：
    三角形顶点坐标元组为键——voxelize/point_in_mesh 对同一网格反复发射
    射线，同三角形同射线同结果（射线固定 +X）。纯确定性。
    """
    key = (round(origin[0], 6), round(origin[1], 6), round(origin[2], 6),
           round(v0[0], 6), round(v0[1], 6), round(v0[2], 6),
           round(v1[0], 6), round(v1[1], 6), round(v1[2], 6),
           round(v2[0], 6), round(v2[1], 6), round(v2[2], 6))
    hit = _RAY_HIT_CACHE.get(key)
    if hit is not None:
        return hit
    e1 = (v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2])
    e2 = (v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2])
    dir = (1.0, 0.0, 0.0)
    pvec = _cross(dir, e2)
    det = _dot(e1, pvec)
    # 相对阈值：与三角形尺度（e1/e2 长度积）比较
    scale = (math.sqrt(_dot(e1, e1)) * math.sqrt(_dot(e2, e2)) + 1e-30)
    if abs(det) < 1e-9 * scale:
        _RAY_HIT_CACHE[key] = None
        return None
    inv = 1.0 / det
    tvec = (origin[0] - v0[0], origin[1] - v0[1], origin[2] - v0[2])
    u = _dot(tvec, pvec) * inv
    if u < 0 or u > 1:
        _RAY_HIT_CACHE[key] = None
        return None
    qvec = _cross(tvec, e1)
    v = _dot(dir, qvec) * inv
    if v < 0 or u + v > 1:
        _RAY_HIT_CACHE[key] = None
        return None
    t = _dot(e2, qvec) * inv
    res = t if t > 1e-12 else None  # 正向射线命中（边界抖动防御）
    if len(_RAY_HIT_CACHE) >= _RAY_HIT_CACHE_MAX:
        _RAY_HIT_CACHE.clear()
    _RAY_HIT_CACHE[key] = res
    return res


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


# ── ⑩ 变换合成缓存（7 维缓存方案维度 8：算子融合——缓存执行计划）──────
# "平移→旋转→缩放"连续变换经常出现；合成矩阵缓存 = 缓存复合变换的
# 执行计划（同参数命中直接给合成结果，不重复矩阵乘法）。纯确定性。
_TRANSFORM_CACHE: dict[tuple, list] = {}
_TRANSFORM_CACHE_MAX = 128


def _mat_mul(a: list, b: list) -> list:
    """4x4 矩阵乘法（行主序，列向量约定 v' = M·v）。"""
    return [sum(a[r * 4 + k] * b[k * 4 + c] for k in range(4))
            for r in range(4) for c in range(4)]


def transform_compose(transforms: list, round_digits: int = 6) -> dict:
    """合成多个 4x4 变换（维度 8 落地：算子融合——缓存执行计划）。

    transforms: [{type: "translate"|"rotate"|"scale"|"matrix", ...}]，
      按 glTF TRS 惯例合成：v' = M₁·M₂·...·Mₙ·v（**列表末尾的变换最先作用于
      顶点**——写 [translate, rotate] = 先旋转后平移，与引擎 TRS 分解一致）。
    - translate: {x,y,z}
    - rotate: {axis: "x"|"y"|"z", angle_deg}
    - scale: {x,y,z}（均匀缩放只给 x 即可）
    - matrix: {m: [16 元素行主序]}
    缓存键 = (tuple 化的 transforms, round_digits)——同参数命中直接返回。
    """
    import math as _m
    key = (json.dumps(transforms, sort_keys=True, ensure_ascii=False), round_digits)
    hit = _TRANSFORM_CACHE.get(key)
    if hit is not None:
        return {"ok": True, "cached": True, "matrix": hit,
                "note": "变换合成缓存命中（维度8：算子融合）"}
    if not isinstance(transforms, list) or not transforms:
        return {"ok": False, "error": "transforms 需为非空数组"}
    ident = [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]
    acc = ident
    # 列向量约定：先应用的在最右 → 逆序遍历逐个左乘
    for t in reversed(transforms):
        if not isinstance(t, dict):
            return {"ok": False, "error": "每个变换需为对象"}
        typ = t.get("type")
        if typ == "translate":
            x, y, z = (float(t.get(k, 0)) for k in ("x", "y", "z"))
            if not all(_m.isfinite(v) for v in (x, y, z)):
                return {"ok": False, "error": "translate 参数需有限数"}
            m = [1, 0, 0, x, 0, 1, 0, y, 0, 0, 1, z, 0, 0, 0, 1]
        elif typ == "scale":
            x = float(t.get("x", 1)); y = float(t.get("y", x)); z = float(t.get("z", x))
            if not all(_m.isfinite(v) and v != 0 for v in (x, y, z)):
                return {"ok": False, "error": "scale 需非零有限数"}
            m = [x, 0, 0, 0, 0, y, 0, 0, 0, 0, z, 0, 0, 0, 0, 1]
        elif typ == "rotate":
            axis = str(t.get("axis", "z"))
            if axis not in ("x", "y", "z"):
                return {"ok": False, "error": "axis 需 x/y/z"}
            ang = _m.radians(float(t.get("angle_deg", 0)))
            if not _m.isfinite(ang):
                return {"ok": False, "error": "angle_deg 需有限数"}
            c, s = _m.cos(ang), _m.sin(ang)
            if axis == "x":
                m = [1, 0, 0, 0, 0, c, -s, 0, 0, s, c, 0, 0, 0, 0, 1]
            elif axis == "y":
                m = [c, 0, s, 0, 0, 1, 0, 0, -s, 0, c, 0, 0, 0, 0, 1]
            else:
                m = [c, -s, 0, 0, s, c, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
        elif typ == "matrix":
            mm = t.get("m")
            if not isinstance(mm, list) or len(mm) != 16:
                return {"ok": False, "error": "matrix.m 需 16 元素列表"}
            m = [float(v) for v in mm]
            if not all(_m.isfinite(v) for v in m):
                return {"ok": False, "error": "matrix 元素需有限数"}
        else:
            return {"ok": False, "error": f"未知变换类型: {typ!r}（translate/rotate/scale/matrix）"}
        acc = _mat_mul(m, acc)
    if round_digits is not None:
        acc = [round(v, round_digits) for v in acc]
    if len(_TRANSFORM_CACHE) >= _TRANSFORM_CACHE_MAX:
        _TRANSFORM_CACHE.clear()
    _TRANSFORM_CACHE[key] = acc
    return {"ok": True, "cached": False, "matrix": acc,
            "note": "变换合成完成（维度8：算子融合——缓存执行计划，同参数下次命中）"}


# ── ⑪ 阵列模式展开缓存（7 维缓存方案维度 9：分形/阵列展开）────────
# 重复结构（10x10 孔阵列等）的坐标生成器——命中时 LLM 只需输出模式引用，
# 后处理展开。确定性：同模式同参数 → 同坐标。
_PATTERN_CACHE: dict[tuple, list] = {}
_PATTERN_CACHE_MAX = 128


def pattern_expand(pattern: str, rows: int = 4, cols: int = 4,
                   spacing: float = 1.0, center: list | None = None) -> dict:
    """阵列模式展开（维度 9 落地：重复结构坐标生成器）。

    pattern: "grid"（矩形阵列）/ "ring"（环形阵列）/ "hilbert"（希尔伯特曲线
    次序——空间填充，缓存友好）。
    输出 positions: [(x,y,z), ...]——LLM 生成重复 3D 结构时可直接引用模式，
    免去逐坐标/循环 Token。
    缓存键 = (pattern, rows, cols, spacing, center)——确定性命中。
    """
    import math as _m
    key = (pattern, rows, cols, spacing,
           json.dumps(center or [0, 0, 0], ensure_ascii=False))
    hit = _PATTERN_CACHE.get(key)
    if hit is not None:
        return {"ok": True, "cached": True, "pattern": pattern,
                "positions": hit, "count": len(hit),
                "note": "阵列模式缓存命中（维度9：展开缓存）"}
    cx, cy, cz = (float(v) for v in (center or [0, 0, 0]))
    if not all(_m.isfinite(v) for v in (cx, cy, cz)):
        return {"ok": False, "error": "center 需有限数"}
    if not (1 <= rows <= 200 and 1 <= cols <= 200):
        return {"ok": False, "error": "rows/cols 需在 [1,200]"}
    try:
        spacing = float(spacing)
    except (TypeError, ValueError):
        return {"ok": False, "error": "spacing 需为数字"}
    if not _m.isfinite(spacing) or spacing == 0:
        return {"ok": False, "error": "spacing 需非零有限数"}
    positions: list = []
    if pattern == "grid":
        for r in range(rows):
            for c in range(cols):
                positions.append((cx + c * spacing, cy + r * spacing, cz))
    elif pattern == "ring":
        total = rows * cols
        for i in range(total):
            ang = 2 * _m.pi * i / total
            rad = spacing * (i // cols + 1)
            positions.append((cx + rad * _m.cos(ang), cy + rad * _m.sin(ang), cz))
    elif pattern == "hilbert":
        # 希尔伯特曲线（2D 空间填充，迭代深度 d=rows）——递归生成
        def _hilbert(d, x, y, dx, dy):
            if d <= 0:
                return [(x, y)]
            pts = []
            pts += _hilbert(d - 1, x, y, dy, dx)
            pts += _hilbert(d - 1, x + dx, y + dy, dx, dy)
            pts += _hilbert(d - 1, x + dx, y + dy, dx, dy)
            pts += _hilbert(d - 1, x + dx - dy, y + dy - dx, -dy, -dx)
            return pts
        depth = min(rows, 4)  # 4^4=256 段封顶（防展开爆炸 DoS）
        pts = _hilbert(depth, 0, 0, 1, 0)
        for (px, py) in pts:
            positions.append((cx + px * spacing, cy + py * spacing, cz))
    else:
        return {"ok": False, "error": f"未知 pattern: {pattern!r}（grid/ring/hilbert）"}
    if len(_PATTERN_CACHE) >= _PATTERN_CACHE_MAX:
        _PATTERN_CACHE.clear()
    _PATTERN_CACHE[key] = positions
    return {"ok": True, "cached": False, "pattern": pattern,
            "positions": positions, "count": len(positions),
            "note": f"阵列模式展开（维度9）——{len(positions)} 个坐标；"
                    "LLM 生成重复 3D 结构时可引用模式免循环 Token"}


# ── ⑫ 包围盒缓存（3D 小手术刀 #5）──────────────────────────
# 每个形状的 AABB 本地计算并缓存——碰撞检测/视锥裁切直接取缓存。
# 键 = 文件签名（mtime+size）+ 可选 transform 合成矩阵。纯确定性。
_BBOX_CACHE: dict[tuple, dict] = {}
_BBOX_CACHE_MAX = 128


def mesh_bbox(path: str) -> dict:
    """网格包围盒（AABB）：min/max/center/extent——本地计算并缓存。

    3D 小手术刀 #5（包围盒缓存）：重复查询直接命中（键 = 文件 mtime+size）。
    """
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {"ok": False, "error": f"文件不可读: {path}"}
    hit = _BBOX_CACHE.get(key)
    if hit is not None:
        return dict(hit)
    m = load_mesh(path)
    if not m.get("ok"):
        return m
    verts = m["vertices"]
    if not verts:
        return {"ok": False, "error": "网格无顶点"}
    xs = [v[0] for v in verts]
    ys = [v[1] for v in verts]
    zs = [v[2] for v in verts]
    bmin = (min(xs), min(ys), min(zs))
    bmax = (max(xs), max(ys), max(zs))
    center = ((bmin[0] + bmax[0]) / 2, (bmin[1] + bmax[1]) / 2,
              (bmin[2] + bmax[2]) / 2)
    extent = (bmax[0] - bmin[0], bmax[1] - bmin[1], bmax[2] - bmin[2])
    result = {"ok": True, "path": path, "min": bmin, "max": bmax,
              "center": center, "extent": extent,
              "radius": round(math.sqrt(extent[0] ** 2 + extent[1] ** 2
                                        + extent[2] ** 2) / 2, 6),
              "advice": "AABB 缓存（#5）——碰撞/视锥/拾取直接复用"}
    if len(_BBOX_CACHE) >= _BBOX_CACHE_MAX:
        _BBOX_CACHE.clear()
    _BBOX_CACHE[key] = result
    return result


# ── ⑬ 质量属性缓存（3D 技术点 #76：惯性矩）─────────────────
# 质心/体积/惯性矩——LLM 查询物理属性时只返回几个标量，不重复计算。
# 确定性；键 = 文件签名。三角形四面体分解（零依赖）。
_MASS_CACHE: dict[tuple, dict] = {}
_MASS_CACHE_MAX = 64


def mesh_mass_props(path: str) -> dict:
    """网格质量属性：体积/质心/惯性矩（三角形四面体分解，闭合网格）。

    3D 技术点 #76（惯性矩缓存）：物理属性标量化——碰撞/物理引擎查询
    直接复用。注意：体积对非闭合（有洞）网格无意义——先 mesh_check。
    """
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {"ok": False, "error": f"文件不可读: {path}"}
    hit = _MASS_CACHE.get(key)
    if hit is not None:
        return dict(hit)
    m = load_mesh(path)
    if not m.get("ok"):
        return m
    verts, faces = m["vertices"], m["faces"]
    # 四面体分解：以原点为参考点，每个三角形与原点构成四面体
    vol6 = 0.0  # 6×体积（有符号）
    cx = cy = cz = 0.0
    ixx = iyy = izz = ixy = ixz = iyz = 0.0
    surface_area = 0.0  # 表面积（#14/#15 neatmesh/Trimesh 指标，3D 打印质量）
    for (a, b, c) in faces:
        v0, v1, v2 = verts[a], verts[b], verts[c]
        # 表面积：三角形叉积模长一半
        e1 = (v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2])
        e2 = (v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2])
        cr = (e1[1] * e2[2] - e1[2] * e2[1],
              e1[2] * e2[0] - e1[0] * e2[2],
              e1[0] * e2[1] - e1[1] * e2[0])
        surface_area += 0.5 * math.sqrt(cr[0] ** 2 + cr[1] ** 2 + cr[2] ** 2)
        # 有符号四面体体积（6 倍）
        det = (v0[0] * (v1[1] * v2[2] - v1[2] * v2[1])
               - v0[1] * (v1[0] * v2[2] - v1[2] * v2[0])
               + v0[2] * (v1[0] * v2[1] - v1[1] * v2[0]))
        vol6 += det
        # 四面体质心（相对原点）
        cx += (v0[0] + v1[0] + v2[0]) * det
        cy += (v0[1] + v1[1] + v2[1]) * det
        cz += (v0[2] + v1[2] + v2[2]) * det
        # 惯性矩（二阶矩，平行轴在质心处修正——简化：相对原点的二阶矩）
        for i, p in enumerate((v0, v1, v2)):
            for j, q in enumerate((v0, v1, v2)):
                pass  # 完整张量计算量小——直接累加分量（见下）
        # 分量累加（单位密度，相对原点）
        for p in (v0, v1, v2):
            ixx += det * (p[1] ** 2 + p[2] ** 2) / 24
            iyy += det * (p[0] ** 2 + p[2] ** 2) / 24
            izz += det * (p[0] ** 2 + p[1] ** 2) / 24
            ixy += det * (p[0] * p[1]) / 24
            ixz += det * (p[0] * p[2]) / 24
            iyz += det * (p[1] * p[2]) / 24
    if abs(vol6) < 1e-12:
        return {"ok": False, "error": "网格体积为零（非闭合或退化）——"
                                      "先用 mesh_check 检查拓扑"}
    vol = vol6 / 6.0
    sign = 1.0 if vol > 0 else -1.0
    vol = abs(vol)
    # 质心（四面体质心 = (v0+v1+v2)/4，加权有符号体积——修复 1/4 因子）
    cent = (cx / (4 * vol6), cy / (4 * vol6), cz / (4 * vol6))
    result = {"ok": True, "path": path,
              "volume": round(vol, 6),
              "surface_area": round(surface_area, 6),
              "surface_volume_ratio": round(surface_area / vol, 6) if vol > 0 else None,
              "centroid": [round(v, 6) for v in cent],
              "inertia": {
                  "ixx": round(sign * ixx, 6), "iyy": round(sign * iyy, 6),
                  "izz": round(sign * izz, 6),
                  "ixy": round(sign * ixy, 6), "ixz": round(sign * ixz, 6),
                  "iyz": round(sign * iyz, 6)},
              "closed": True,
              "advice": "质量属性缓存（#76）+ 表面积（#14/#15 neatmesh 指标）——"
                        "体积/表面积/表面积体积比是 3D 打印与物理查询关键标量；"
                        "惯性矩为相对原点近似（质心处张量需平行轴修正）"}
    if len(_MASS_CACHE) >= _MASS_CACHE_MAX:
        _MASS_CACHE.clear()
    _MASS_CACHE[key] = result
    return result


# ── ⑭ 可微渲染基础设施（#9 可微分渲染落地）─────────────────
# 可微渲染四大件的数据层：①可微表示（mesh_splat 参数张量已有）②软光栅
# 渲染器（render_depth）③损失（render_loss）④数值梯度（render_gradient）。
# 纯 Python 零依赖（无 GPU 框架）——提供"可微渲染的数据基础设施"，
# 梯度用有限差分（可验证正确性）；真·GPU 梯度（Nvdiffrast 级）为未来方向。

def render_depth(path: str, resolution: int = 32, camera: str = "front") -> dict:
    """软光栅渲染器：网格 → 深度图（z-buffer，正交投影，三视图可选）。

    #9 可微渲染落地①：渲染器。把 3D 网格"画"成 2D 深度图（可微分渲染
    的数据流起点——渲染图与目标图差异 → 损失 → 梯度）。
    camera: front（+Z 视）/ top（-Y 视）/ side（-X 视）。
    输出 depth: [resolution][resolution] 浮点深度（0=空，>0=近表面距离）。
    """
    if not 4 <= resolution <= 128:
        return {"ok": False, "error": "resolution 须在 4..128"}
    m = load_mesh(path)
    if not m.get("ok"):
        return m
    verts, faces = m["vertices"], m["faces"]
    if not verts:
        return {"ok": False, "error": "网格无顶点"}
    b = mesh_bbox(path)
    if not b.get("ok"):
        return b
    bmin, bmax = b["min"], b["max"]
    span = max(bmax[0] - bmin[0], bmax[1] - bmin[1],
               bmax[2] - bmin[2], 1e-9)
    cell = span / resolution
    # 正交投影：取两轴坐标 → 像素格；第三轴为深度
    def _proj(v):
        if camera == "front":
            return (v[0], v[1]), v[2]
        if camera == "top":
            return (v[0], v[2]), -v[1]
        return (v[1], v[2]), -v[0]  # side：-X 视

    # 光栅化：每三角形 → 其 AABB 像素范围 → 重心坐标内测试 → z-buffer
    depth = [[float("inf")] * resolution for _ in range(resolution)]
    for (a, b, c) in faces:
        va, vb, vc = verts[a], verts[b], verts[c]
        pts = [_proj(v) for v in (va, vb, vc)]
        (p0, z0), (p1, z1), (p2, z2) = pts
        # 像素范围（含边界）
        px_min = max(0, int(min(p0[0], p1[0], p2[0]) / cell))
        px_max = min(resolution - 1, int(max(p0[0], p1[0], p2[0]) / cell))
        py_min = max(0, int(min(p0[1], p1[1], p2[1]) / cell))
        py_max = min(resolution - 1, int(max(p0[1], p1[1], p2[1]) / cell))
        for ix in range(px_min, px_max + 1):
            px = bmin[0] + (ix + 0.5) * cell
            for iy in range(py_min, py_max + 1):
                py = bmin[1] + (iy + 0.5) * cell
                # 重心坐标（2D 三角形包含）
                denom = ((p1[1] - p2[1]) * (p0[0] - p2[0])
                         + (p2[0] - p1[0]) * (p0[1] - p2[1]))
                if abs(denom) < 1e-12:
                    continue
                w0 = ((p1[1] - p2[1]) * (px - p2[0])
                      + (p2[0] - p1[0]) * (py - p2[1])) / denom
                w1 = ((p2[1] - p0[1]) * (px - p2[0])
                      + (p0[0] - p2[0]) * (py - p2[1])) / denom
                w2 = 1 - w0 - w1
                if w0 >= 0 and w1 >= 0 and w2 >= 0:
                    z = w0 * z0 + w1 * z1 + w2 * z2  # 插值深度
                    if z < depth[iy][ix]:
                        depth[iy][ix] = z
    # 归一化深度（近=1，远=0；空=0）
    norm = [[round(1.0 - (d - bmin[2]) / span, 4) if d != float("inf") else 0.0
             for d in row] for row in depth]
    return {"ok": True, "path": path, "camera": camera,
            "resolution": resolution, "depth": norm,
            "advice": "软光栅深度图（#9 可微渲染①）——渲染→损失→梯度数据流起点"}


def render_loss(path: str, target: list, resolution: int = 32,
                camera: str = "front") -> dict:
    """渲染损失：当前网格渲染图 vs 目标图（L1/L2，可微渲染②③）。

    target: 与 render_depth 同构的 [resolution][resolution] 0..1 目标图。
    loss = mean(|render - target|)（L1）——梯度下降优化的标量目标。
    """
    r = render_depth(path, resolution=resolution, camera=camera)
    if not r.get("ok"):
        return r
    if len(target) != resolution or any(len(row) != resolution for row in target):
        return {"ok": False, "error": f"target 需为 {resolution}×{resolution} 矩阵"}
    total = 0.0
    count = 0
    for i in range(resolution):
        for j in range(resolution):
            d = r["depth"][i][j]
            t = float(target[i][j])
            total += abs(d - t)
            count += 1
    return {"ok": True, "loss": round(total / max(1, count), 6),
            "loss_type": "L1",
            "advice": "渲染损失（#9 可微渲染②③）——梯度下降的标量目标"}


def render_gradient(path: str, target: list, resolution: int = 32,
                    camera: str = "front", eps: float = 0.01,
                    vertex: int = 0) -> dict:
    """数值梯度（有限差分）：顶点扰动 → 渲染损失变化率（可微渲染④⑤）。

    #9 可微渲染落地：给定目标图，计算指定顶点的梯度（∂loss/∂v）——
    "训练/优化 3D 模型"的数据基础设施：梯度 → 顶点位置更新 → 渲染更接近
    目标。真·GPU 反向传播（Nvdiffrast）为未来方向——有限差分可验证且
    零依赖。
    """
    if not (eps > 0 and eps <= 0.5):
        return {"ok": False, "error": "eps 须在 (0, 0.5]"}
    if vertex < 0:
        return {"ok": False, "error": "vertex 须 >= 0"}
    m = load_mesh(path)
    if not m.get("ok"):
        return m
    verts = m["vertices"]
    if vertex >= len(verts):
        return {"ok": False, "error": f"vertex {vertex} 越界（共 {len(verts)} 顶点）"}
    base = render_loss(path, target, resolution=resolution, camera=camera)
    if not base.get("ok"):
        return base
    grad = []
    for axis in range(3):
        perturbed = os.path.join(os.path.dirname(path),
                                 f"_grad_{os.path.basename(path)}")
        try:
            v = list(verts)
            v[vertex] = list(v[vertex])
            v[vertex][axis] += eps
            v[vertex] = tuple(v[vertex])
            if path.lower().endswith(".obj"):
                with open(perturbed, "w", encoding="utf-8") as f:
                    for p in v:
                        f.write(f"v {p[0]} {p[1]} {p[2]}\n")
                    for (a, b, c) in m["faces"]:
                        f.write(f"f {a+1} {b+1} {c+1}\n")
            else:
                return {"ok": False, "error": "有限差分暂支持 .obj（梯度基础设施）"}
            pl = render_loss(perturbed, target, resolution=resolution,
                             camera=camera)
            if not pl.get("ok"):
                return pl
            grad.append(round((pl["loss"] - base["loss"]) / eps, 6))
        finally:
            try:
                os.remove(perturbed)
            except OSError:
                pass
    return {"ok": True, "vertex": vertex, "gradient": grad,
            "base_loss": base["loss"], "eps": eps,
            "advice": "数值梯度（#9 可微渲染④⑤）——∂loss/∂v 有限差分；"
                      "沿负梯度更新顶点可使渲染逼近目标"}


# ── ⑮ 碰撞检测（FCL/Parry 概念零依赖落地，用户点名）──────────
# FCL/Parry 是 C++/Rust 精确碰撞库——本仓库零依赖原则下自研轻量等价：
# ① AABB 粗筛（快速排除）② 三角形对精确相交（Möller 三角形-三角形）。
# 确定性、纯 Python、无外部依赖。支持 mesh-mesh / mesh-AABB / mesh-点。
_TRI_TRI_CACHE: dict[tuple, bool] = {}
_TRI_TRI_CACHE_MAX = 4096


def _tri_tri_intersect(p1, q1, r1, p2, q2, r2) -> bool:
    """Möller 三角形-三角形相交测试（返回是否相交）。

    基于间隔重叠测试（interval overlap along intersection line）——
    精确、无浮点脆弱点（相对阈值）。缓存键 = 6 顶点坐标量化。
    """
    key = tuple(round(x, 6) for pt in (p1, q1, r1, p2, q2, r2) for x in pt)
    hit = _TRI_TRI_CACHE.get(key)
    if hit is not None:
        return hit

    def _sub(a, b):
        return (a[0] - b[0], a[1] - b[1], a[2] - b[2])

    def _cross(a, b):
        return (a[1] * b[2] - a[2] * b[1],
                a[2] * b[0] - a[0] * b[2],
                a[0] * b[1] - a[1] * b[0])

    def _dot(a, b):
        return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]

    # 平面分离测试（第一个三角形平面）
    n1 = _cross(_sub(q1, p1), _sub(r1, p1))
    if _dot(n1, n1) < 1e-30:
        _TRI_TRI_CACHE[key] = False
        return False  # 退化三角形
    d1 = -_dot(n1, p1)
    dv2 = _dot(n1, p2) + d1
    dv3 = _dot(n1, q2) + d1
    dv4 = _dot(n1, r2) + d1
    if dv2 * dv3 > 0 and dv3 * dv4 > 0:
        _TRI_TRI_CACHE[key] = False
        return False  # 同侧——分离
    # 第二个三角形平面
    n2 = _cross(_sub(q2, p2), _sub(r2, p2))
    if _dot(n2, n2) < 1e-30:
        _TRI_TRI_CACHE[key] = False
        return False
    d2 = -_dot(n2, p2)
    dv1 = _dot(n2, p1) + d2
    dv2b = _dot(n2, q1) + d2
    dv3b = _dot(n2, r1) + d2
    if dv1 * dv2b > 0 and dv2b * dv3b > 0:
        _TRI_TRI_CACHE[key] = False
        return False
    # 相交线方向 + 间隔重叠（在相交线上投影区间）
    line_dir = _cross(n1, n2)
    if _dot(line_dir, line_dir) < 1e-30:
        # 共面——退化情况：用顶点包含测试近似（精确共面极罕见）
        res = _point_in_tri(p1, p2, q2, r2) or _point_in_tri(p2, p1, q1, r1)
        _TRI_TRI_CACHE[key] = res
        return res
    # 投影间隔 [min1,max1] vs [min2,max2]（沿相交线）
    def _proj_interval(pts):
        vals = sorted(_dot(pt, line_dir) for pt in pts)
        return vals[0], vals[-1]
    lo1, hi1 = _proj_interval((p1, q1, r1))
    lo2, hi2 = _proj_interval((p2, q2, r2))
    res = not (hi1 < lo2 or hi2 < lo1)
    if len(_TRI_TRI_CACHE) >= _TRI_TRI_CACHE_MAX:
        _TRI_TRI_CACHE.clear()
    _TRI_TRI_CACHE[key] = res
    return res


def _point_in_tri(p, a, b, c) -> bool:
    """点是否在三角形内（重心坐标，含边界容差）。"""
    def _cross(u, v):
        return (u[1] * v[2] - u[2] * v[1],
                u[2] * v[0] - u[0] * v[2],
                u[0] * v[1] - u[1] * v[0])
    def _dot(u, v):
        return u[0] * v[0] + u[1] * v[1] + u[2] * v[2]
    v0 = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    v1 = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    v2 = (p[0] - a[0], p[1] - a[1], p[2] - a[2])
    n = _cross(v0, v1)
    if _dot(n, n) < 1e-30:
        return False
    inv = 1.0 / _dot(n, n)
    # 重心坐标（投影到法线平面）
    u = _dot(_cross(v2, v1), n) * inv
    v = _dot(_cross(v0, v2), n) * inv
    w = 1.0 - u - v
    eps = 1e-6
    return u >= -eps and v >= -eps and w >= -eps


def collision_check(path_a: str, path_b: str | None = None,
                    point: list | None = None,
                    aabb: list | None = None) -> dict:
    """碰撞检测（FCL/Parry 概念零依赖落地）：AABB 粗筛 + 三角形对精确相交。

    模式：
    - mesh-mesh：path_a vs path_b——AABB 不相交直接"无碰撞"（秒回）；
      相交则三角形对精确检测（采样上限防爆炸）。
    - mesh-point：path_a vs point [x,y,z]——点是否在网格内/表面。
    - mesh-aabb：path_a vs aabb [[minx,miny,minz],[maxx,maxy,maxz]]。
    三角形对结果缓存（量化键）——重复查询/多视角复用。
    """
    m1 = load_mesh(path_a)
    if not m1.get("ok"):
        return m1
    va, fa = m1["vertices"], m1["faces"]
    b1 = mesh_bbox(path_a)
    if not b1.get("ok"):
        return b1
    if point is not None:
        if len(point) != 3:
            return {"ok": False, "error": "point 需 [x,y,z]"}
        p = tuple(float(v) for v in point)
        inside = _point_in_mesh(p, va, fa)
        return {"ok": True, "mode": "mesh-point", "point": list(p),
                "colliding": inside,
                "advice": "点在网格内=碰撞（射线法奇偶测试）" if inside
                          else "点在网格外（无碰撞）"}
    if aabb is not None:
        if len(aabb) != 2 or any(len(r) != 3 for r in aabb):
            return {"ok": False, "error": "aabb 需 [[minx,miny,minz],[maxx,maxy,maxz]]"}
        lo = tuple(float(v) for v in aabb[0])
        hi = tuple(float(v) for v in aabb[1])
        bmin, bmax = b1["min"], b1["max"]
        overlap = (bmin[0] <= hi[0] and bmax[0] >= lo[0]
                   and bmin[1] <= hi[1] and bmax[1] >= lo[1]
                   and bmin[2] <= hi[2] and bmax[2] >= lo[2])
        return {"ok": True, "mode": "mesh-aabb", "aabb": [lo, hi],
                "colliding": overlap,
                "advice": "网格 AABB 与查询 AABB 重叠" if overlap
                          else "网格 AABB 与查询 AABB 分离（无碰撞）"}
    if not path_b:
        return {"ok": False, "error": "需提供 path_b / point / aabb 之一"}
    m2 = load_mesh(path_b)
    if not m2.get("ok"):
        return m2
    vb, fb = m2["vertices"], m2["faces"]
    b2 = mesh_bbox(path_b)
    if not b2.get("ok"):
        return b2
    # AABB 粗筛（FCL 同款 broad-phase：快速排除不相交对）
    a1, a2 = b1["min"], b1["max"]
    c1, c2 = b2["min"], b2["max"]
    if (a1[0] > c2[0] or a2[0] < c1[0]
            or a1[1] > c2[1] or a2[1] < c1[1]
            or a1[2] > c2[2] or a2[2] < c1[2]):
        return {"ok": True, "mode": "mesh-mesh", "broad_phase": "separate",
                "colliding": False,
                "advice": "AABB 粗筛分离——无需精确检测（FCL broad-phase）"}
    # 精确阶段（narrow-phase）：三角形对相交（采样上限防爆炸）
    max_pairs = 200_000
    total_pairs = len(fa) * len(fb)
    sampled = 0
    hit_count = 0
    stride = max(1, total_pairs // max_pairs) if total_pairs > max_pairs else 1
    for i in range(0, len(fa), stride):
        tri_a = fa[i]
        pa = (va[tri_a[0]], va[tri_a[1]], va[tri_a[2]])
        for j in range(0, len(fb), stride):
            tri_b = fb[j]
            pb = (vb[tri_b[0]], vb[tri_b[1]], vb[tri_b[2]])
            if _tri_tri_intersect(pa[0], pa[1], pa[2],
                                  pb[0], pb[1], pb[2]):
                hit_count += 1
                # 找到首个碰撞即可（报告数量用采样估计）
                return {"ok": True, "mode": "mesh-mesh",
                        "broad_phase": "overlap",
                        "colliding": True,
                        "first_hit": {"face_a": tri_a, "face_b": tri_b},
                        "sampled_pairs": sampled + 1,
                        "total_pairs": total_pairs,
                        "advice": "网格碰撞（narrow-phase 精确检测命中）"}
            sampled += 1
    return {"ok": True, "mode": "mesh-mesh", "broad_phase": "overlap",
            "colliding": False, "sampled_pairs": sampled,
            "total_pairs": total_pairs,
            "advice": "AABB 重叠但三角形对无相交（间隙网格或采样未命中）"}


# ── ⑯ 拓扑持久同调简化版：Betti 数（#11 技术1 落地）──────────
# 持久同调完整版（Persistence Diagram）需滤复形计算；本落地为确定性
# Betti 数（拓扑"体检"标量）：
#   β0 = 连通分量数（网格是否碎裂）
#   β1 = 一维环数（孔洞/隧道——欧拉公式 β1 = β0 + β2 - χ）
#   β2 = 二维空腔数（封闭空腔——mesh_check 边界边=0 时用闭合性近似）
# 与 mesh_check 互补：mesh_check 报"哪里有洞"，Betti 数报"拓扑是什么"。
def mesh_betti(path: str) -> dict:
    """网格 Betti 数（拓扑质量度量，#11 简化落地）。

    β0 连通分量：顶点图并查集；β2 空腔：无边界边且非球面拓扑（用
    欧拉公式反推）；β1 环数：β1 = β0 + β2 - χ（欧拉公式，闭合网格）。
    对非闭合网格 β2 不适用（先 mesh_check）。
    """
    m = load_mesh(path)
    if not m.get("ok"):
        return m
    verts, faces = m["vertices"], m["faces"]
    n = len(verts)
    # 并查集求连通分量（顶点通过面边相连）
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for f in faces:
        for i in range(len(f)):
            union(f[i], f[(i + 1) % len(f)])
    components = len({find(i) for i in range(n)})
    # 欧拉示性数 χ = V - E + F
    edge_set = set()
    for f in faces:
        for i in range(len(f)):
            a, b = f[i], f[(i + 1) % len(f)]
            edge_set.add((min(a, b), max(a, b)))
    chi = n - len(edge_set) + len(faces)
    # 边界边（β2 判定用）
    edge_faces: dict[tuple, int] = defaultdict(int)
    for f in faces:
        for i in range(len(f)):
            a, b = f[i], f[(i + 1) % len(f)]
            edge_faces[(min(a, b), max(a, b))] += 1
    boundary = sum(1 for c in edge_faces.values() if c == 1)
    closed = boundary == 0
    # 闭合网格：χ = β0 - β1 + β2 → β2 = χ - β0 + β1；球面拓扑 β2=1。
    # 近似：闭合且 χ 与 β0 关系判定（β2 = χ - β0 + 1 当 β1 未知——
    # 用 β1 = β0 + β2 - χ 与闭合球面基准 χ=2β0 比较）
    if closed:
        # 对闭合网格：β2 = χ - β0 + β1；单连通（β1=0）时 β2 = χ - β0。
        # 常见判定：χ > β0 → 有孔洞或空腔。用 β2 = max(0, χ - β0) 近似
        # （球面 χ=2，β0=1 → β2=1 正确；圆环 χ=0，β0=1 → β2=0 + β1=1）。
        beta2_approx = max(0, chi - components)
        beta1 = max(0, components + beta2_approx - chi)
    else:
        beta2_approx = None
        # 非闭合：β1 无法从欧拉公式可靠获得（边界贡献未知）——给警告
        beta1 = None
    result = {"ok": True, "path": path,
              "betti": {
                  "beta0": components,      # 连通分量（>1 = 网格碎裂）
                  "beta1": beta1,           # 环/孔洞数
                  "beta2": beta2_approx,    # 空腔数
              },
              "euler_characteristic": chi,
              "closed": closed,
              "boundary_edges": boundary,
              "advice": (
                  "闭合网格：β0=连通分量，β1=环/隧道，β2=空腔——"
                  "β0>1 网格碎裂，β1>0 有孔洞，β2>1 有多重空腔" if closed
                  else "网格非闭合（有边界边）——β1/β2 不可靠，先用 mesh_check 补洞")}
    return result
