# 可微渲染 + 新一代几何表示——工具集

> 2026-08-15 · 概念落地（务实——不交付假的 GPU 渲染）

---

## 概念映射

| 前沿方向 | 本工具集落地 |
|---|---|
| **TetSphere Splatting**（四面体球变形建模——解决破面/非流形） | `mesh_check`：拓扑健康报告（非流形边/破面边界边/孤立顶点/重复顶点） |
| **非经典 NURBS**（设计时间↓70-85%、多边形↓60%） | `mesh_optimize`：精简建议（welding 重复顶点合并 + 共面面片合并候选 + 目标精简率） |
| **Triangle Splatting**（三角面片→可训练参数，无需后处理引擎即用） | `mesh_splat`：三角面片→可训练参数表（顶点/法线/面索引张量结构） |
| **Radiant Foam**（体素光线追踪——标准 GPU 高斯泼溅速度） | `voxelize`：网格体素化（包围盒采样 + Möller–Trumbore 相交——占用表示） |

## 工具

### mesh_check（拓扑质量）
```
输入：.obj/.stl（二进制）/.ply → 输出：流形判定 + 问题清单
  - boundary_hole（破面——边界边）
  - nonmanifold（>2 面共享边——引擎渲染/物理问题）
  - isolated_vertex / duplicate_vertices
```

### mesh_optimize（表示效率）
```
welding（重复位置顶点合并）→ 共面面片合并候选（共享边+同法线）→ 目标精简率建议
```

### mesh_splat（可训练参数）
```
vertex_tensor [V,3] + face_index_tensor [F,3] + normal_tensor [F,3]
——"三角面片变成可训练参数"的数据基础设施（param_count 统计）
```

### voxelize（体素表示）
```
包围盒网格采样（分辨率 4..128）+ 射线法包含测试 → 占用体素/密度
——Radiant Foam 概念基础（光线追踪/碰撞）
```

## 格式支持

- `.obj`（文本——v/vn/vt/f，fan 三角化）
- `.stl`（二进制——84 字节头 + 50 字节/三角形）
- `.ply`（ASCII——header + 顶点/面）
- `.glb/.gltf` 需第三方库——**标注未支持**（诚实）

## 验证

| 项 | 结果 |
|---|---|
| 全量测试 | **319 passed**（314 + 5 几何） |
| 工具数 | 71 → **75**（mesh_check/mesh_optimize/mesh_splat/voxelize） |
| 关键用例 | 四面体流形 ✓ / 单面片破面检出 ✓ / 重复顶点 welding ✓ / 参数表形状 ✓ / 立方体体素占用 ✓ |

## 未来方向（诚实标注）

- 真·Triangle Splatting（GPU 梯度优化不透明三角形）——需 PyTorch/nvdiffrast 类框架——本实现提供参数提取层
- 真·TetSphere 变形建模 / NURBS 重拓扑——本实现提供拓扑检测 + 精简建议（规则驱动）
- 体素光线追踪渲染——本实现提供体素占用基础
