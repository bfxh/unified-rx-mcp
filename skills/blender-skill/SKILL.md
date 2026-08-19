---
name: blender-skill
description: "Blender 建模/材质/动画/渲染/GLB 导出技能（Blender 4.x/5.x，bpy + bmesh 官方 Python API）。在用户要求创建/修改 3D 模型、UV 展开、材质与 PBR 贴图、骨骼绑定与动画、EEVEE/Cycles 渲染、blend→GLB/glTF 导出（Bevy/Three.js/Godot 管线）、程序化生成（节点/脚本）、烘焙与 LOD 时调用。含反模式清单（非流形几何、全三角化、未应用变换、缩放导入、命名混乱）。Invoke when modeling, texturing, rigging, animating, rendering, or exporting 3D assets with Blender, or when generating bpy/bmesh Python scripts."
version: "1.0"
runAs: subagent
allowed-tools: read_file, write_file, edit_file, bash
---

# Blender 技能（bpy/bmesh 全流程）

## 定位

用官方 Python API（`bpy` / `bmesh`）在 Blender 内完成**建模 → 材质 → 绑定 → 动画 → 渲染 → 导出**全流程。
本技能产出**可执行脚本**（Blender 内置 Text Editor 运行，或 `blender --background --python script.py`），
并给出**人工验证步骤**（Blender 是 GUI 软件，脚本正确 ≠ 视觉正确——必须截图/视口确认）。

## 何时使用

- 创建/修改 3D 模型（低多边形/高模/硬表面/有机）
- UV 展开与纹理烘焙、PBR 材质（Base Color/Normal/Roughness/Metallic）
- 骨骼绑定（armature）与动画（关键帧/动作）
- EEVEE / Cycles 渲染与出图
- 导出 GLB/glTF（Bevy、Three.js、Godot 通用管线）
- 程序化生成（几何节点 / bpy 脚本批量生成）

## 核心执行模式（必须遵守）

### 1. 场景清理与命名（防"模型名混乱"反模式）

```python
import bpy
# 清空默认场景，只保留一个 collection
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete()
# 命名规范：prefix_asset_variant（如 env_rock_low）
```

### 2. 建模（bmesh 优先——比 bpy.ops 快且可控）

```python
import bmesh
mesh = bmesh.new()
# 顶点/面/挤出/倒角
v = mesh.verts.new((0, 0, 0))
mesh.faces.new([...])
bmesh.ops.extrude_face_region(mesh, geom=[face])
bmesh.ops.bevel(mesh, geom=[edge], offset=0.05, segments=2)
mesh.normal_update()
# 写回 mesh
bm = bmesh.new()
bm.from_mesh(obj.data)
# ... 操作 ...
bm.to_mesh(obj.data)
bm.free()
```

### 3. 应用变换（必做——导出前不应用变换=GLB 里缩放/旋转错误）

```python
bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
# 或 data 级别
obj.data.transform(obj.matrix_world)
```

### 4. 材质（Principled BSDF + PBR 贴图）

```python
mat = bpy.data.materials.new("mat_rock")
mat.use_nodes = True
bsdf = mat.node_tree.nodes["Principled BSDF"]
bsdf.inputs["Base Color"].default_value = (0.6, 0.5, 0.4, 1)
bsdf.inputs["Roughness"].default_value = 0.8
# 贴图：Image Texture 节点 → BSDF 对应输入
```

### 5. 绑定与动画（armature）

```python
arm = bpy.data.objects.new("Armature", bpy.data.armatures.new("Armature"))
bpy.context.scene.collection.objects.link(arm)
bpy.context.view_layer.objects.active = arm
bpy.ops.object.mode_set(mode="EDIT")
bone = arm.data.edit_bones.new("bone_root")
bone.head = (0, 0, 0); bone.tail = (0, 0, 1)
# 权重：顶点组 + armature modifier
```

### 6. 渲染（EEVEE 快 / Cycles 真）

```python
scene.render.engine = "CYCLES"   # 或 "BLENDER_EEVEE_NEXT"
scene.cycles.samples = 128
scene.render.resolution_x = 1920
scene.render.resolution_y = 1080
scene.render.filepath = "//out/render.png"
bpy.ops.render.render(write_still=True)
```

### 7. GLB/glTF 导出（Bevy/Three/Godot 管线标准）

```python
bpy.ops.export_scene.gltf(
    filepath="//out/model.glb",
    export_format="GLB",
    use_selection=True,        # 只导选中
    export_apply=True,         # 应用变换（等价 transform_apply）
    export_yup=True,           # glTF 是 Y-up，Blender 是 Z-up——必须
    export_materials="EXPORT", # 导出材质（PBR）
)
```

## 反模式清单（生成后必须自查）

| 反模式 | 后果 | 修正 |
|---|---|---|
| 不应用变换就导出 | GLB 缩放/旋转错乱 | `export_apply=True` 或 `transform_apply` |
| 忘 `export_yup` | 模型在 Three/Bevy 里躺倒 | glTF 导出 Y-up 必须开 |
| 非流形几何（开口面/零面积面） | 布尔失败/切片裂缝/引擎渲染破面 | `bmesh.ops` 后 `mesh.normal_update()`；`bpy.ops.mesh.select_all` + `bpy.ops.mesh.check`（Mesh Check 插件） |
| 命名含中文/空格/特殊字符 | 引擎资源路径报错 | 命名 `[a-z0-9_]`，前缀分类 |
| 导出整场景而非选区 | GLB 体积爆炸/混入灯光相机 | `use_selection=True` |
| 材质未应用 → 默认灰 | 引擎里全灰 | 每个 object 至少指定一个 material slot |
| 动画没 bake | 程序化驱动动画导出丢失 | `bpy.ops.nla.bake` 或 keyframe 明确写入 |

## 验证步骤（脚本跑完必做——脚本正确 ≠ 视觉正确）

1. `blender --background --python script.py` 无报错退出（exit 0）
2. 检查导出文件存在且大小合理（`ls -la out/model.glb`）
3. GUI 打开 .blend 人工确认：视口模型无破面、材质有颜色、动画能播
4. 有 `blender_verify` 工具时调用它做截图+工具栏分析（用户规则：搞完 Blender 相关改动必须实地查看）
5. 引擎侧导入验证：Bevy `AssetServer` / Three `GLTFLoader` 能加载且朝向正确

## 常用资源

- Blender Python API：`bpy.data`（数据块）/ `bpy.context`（上下文）/ `bpy.ops`（操作符）
- bmesh 文档：`bmesh.ops.*`（几何操作）/ `bmesh.types.BMesh`（数据结构）
- 导出：`bpy.ops.export_scene.gltf`（glTF 2.0 官方导出器）
- 版本差异：Blender 4.x 用 EEVEE-Next、Blender 5.x 材质系统有变化——先查 `bpy.app.version`
