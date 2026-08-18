# 开源几何内核·实际化执行计划（OPEN GEOMETRY PLAN）

> 2026-08-15 · 用户要求：**要实际代码/实际工具，不是文档**——本计划列出全部待办，
> 逐项产出**可运行的代码**并**实机跑通**（非仅 pytest）。
> 原则：开源零依赖（纯 std 库，不引入商业软件）。

## 一、已有实际工具（2026-08-15 快照：81 核心；现 101 核心——文档保留当时状态）

| # | 工具 | 模块 | 状态 |
|---|---|---|---|
| 1 | mesh_check | geometry_tools.py | pytest ✓ · 实机待跑 |
| 2 | mesh_optimize | geometry_tools.py | pytest ✓ · 实机待跑 |
| 3 | mesh_splat | geometry_tools.py | pytest ✓ · 实机待跑 |
| 4 | voxelize | geometry_tools.py | pytest ✓ · 实机待跑 |
| 5 | geometry_exchange（Rhino.Inside） | geometry_tools.py | pytest ✓ · 实机待跑 |
| 6 | half_edge（Manifold3D） | geometry_tools.py | pytest ✓ · 实机待跑 |
| 7 | mesh_union（PicoGK） | geometry_tools.py | pytest ✓ · 实机待跑 |
| 8 | mesh_clip（真·CSG 基础） | geometry_tools.py | pytest ✓ · 实机待跑 |
| 9 | geom_graph（Grasshopper DSL） | geometry_tools.py | pytest ✓ · 实机待跑 |
| 10 | geom_example（Program.cs 概念） | geometry_tools.py | pytest ✓ · **示例实机待跑** |

## 二、待执行项（按序做——全部产出实际代码）

1. **实机跑通 10 个几何工具**（MCP 层 `_call` 实际调用一遍——临时网格文件——
   输出全 JSON 解析确认非 Error）——发现问题即修复
2. **geom_example 生成 3 个示例实际运行**（`python geom_xxx_example.py`——
   真实可执行代码——断言输出"示例通过"）
3. **mesh_clip 升级**：支持多边形面（当前三角扇——四边/五边输入面正确处理）
4. **half_edge 升级**：add_edge 邻接查询 API（调用方拿半边结构做操作——
   Manifold3D 概念的实际操控接口）
5. **mesh_boolean 基础**：AABB 相交检测 + 相交面标记（CSG 并/交/差的
   检测层——为真·CSG 铺路——诚实标注裁剪层为未来）
6. **voxelize 升级**：占用率 + 表面体素提取（Radiant Foam 概念——
   表面点云输出——实际数据可用）
7. **修复项**（实机运行发现的所有问题——逐一修复 + 复验）

## 三、验收

- 10 工具实机全过（非 Error 输出）
- 3 示例实际运行"示例通过"
- 升级项（3-6）pytest + 实机双验证
- 全量 331+ 绿 · 提交推送 · E 盘同步
