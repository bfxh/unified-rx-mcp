---
name: maya-skill
description: "Autodesk Maya 3D 技能（MEL + Python API `cmds`/`pymel`）：建模/UV/材质/绑定/动画/渲染/导出（FBX/OBJ/ABC 缓存）。在用户要求 Maya 场景操作、角色建模与绑定（skeleton/IK/权重）、动画（关键帧/驱动/动画层）、Arnold 渲染、FBX 导出（Unity/Unreal 管线）、Python 脚本自动化 Maya 时调用。含反模式清单（命名空间污染、历史未清理、单位不一致、FBX 导出朝向错误）。Invoke when scripting, modeling, rigging, animating, or exporting from Autodesk Maya."
version: "1.0"
runAs: subagent
allowed-tools: read_file, write_file, edit_file, bash
---

# Maya 技能（MEL + Python cmds）

## 定位

Autodesk Maya 是电影/游戏行业的 DCC 标准。本技能用官方脚本接口（MEL 与 Python `cmds`/`pymel`）
完成：**场景管理 → 建模 → UV → 材质（Arnold）→ 绑定 → 动画 → 渲染 → FBX/ABC 导出**。
产出可在 Maya Script Editor（Python 标签页）直接运行的脚本，并给人工视口验证步骤。

> 执行方式：Maya Script Editor 内运行，或 `mayapy script.py`（Maya 自带 Python 解释器，
> 仅 Python API 可用，无 GUI）。

## 何时使用

- 角色建模/硬表面建模（poly 工具集）
- 绑定（joints/skeleton/IK handles/skinCluster 权重）
- 动画（关键帧、驱动关键帧 driven keys、动画层、时间编辑器）
- Arnold 材质与渲染
- FBX/OBJ/ABC（Alembic 缓存）导出——Unity/Unreal 管线
- Python 批量自动化（批处理命名/清理/导出）

## 核心执行模式（必须遵守）

### 1. 场景清理与单位（防"单位不一致"反模式——FBX 导入引擎后尺寸全错）

```python
import maya.cmds as cmds
cmds.select(all=True)
cmds.delete()
cmds.currentUnit(linear="cm")     # Maya 默认 cm；游戏引擎常用 cm（Unity）或 m（Unreal 需换算）
cmds.currentUnit(time="ntsc")     # 24fps 电影 / "game" 30fps / "pal" 25fps
# 检查现有场景单位
print(cmds.currentUnit(query=True, linear=True))
```

### 2. 建模（poly 基础操作）

```python
cube = cmds.polyCube(name="hero_body")[0]
cmds.polyExtrudeFacet(cube, translation=[0, 1, 0], divisions=2)
cmds.polyBevel(cube, offset=0.05, segments=2)
cmds.polySmooth(cube, divisions=1)     # 细分
# 对称建模
cmds.polyMirrorFacet(cube, direction=0, mergeMode=1)  # 或 mirrorGeometry
```

### 3. 材质（Arnold aiStandardSurface）

```python
shader = cmds.shadingNode("aiStandardSurface", asShader=True, name="mat_body")
cmds.setAttr(shader + ".baseColor", 0.6, 0.5, 0.4, type="double3")
cmds.setAttr(shader + ".base", 1)            # base weight
cmds.setAttr(shader + ".specularRoughness", 0.6)
cmds.select(cube)
cmds.hyperShade(assign=shader)
```

### 4. 绑定（joints + skinCluster）

```python
j1 = cmds.joint(name="joint_root", position=(0, 0, 0))
cmds.select(clear=True)
j2 = cmds.joint(name="joint_mid", position=(0, 5, 0))
cmds.select(clear=True)
j3 = cmds.joint(name="joint_tip", position=(0, 10, 0))
cmds.select([j1, j2, j3], replace=True)
cmds.skinCluster("joint_root", "joint_mid", "joint_tip", cube, name="skin1")
# IK（腿部/手臂常用）
ik = cmds.ikHandle(startJoint="joint_root", endEffector="joint_tip", solver="ikRPsolver")
```

### 5. 动画（关键帧）

```python
cmds.select("joint_mid", replace=True)
cmds.setKeyframe(attribute="rotateX", t=1, v=0)
cmds.setKeyframe(attribute="rotateX", t=24, v=45)   # 24 帧处转 45°
# 循环动画：切线设置（防"动画滑步/突跳"）
cmds.selectKey("joint_mid", attribute="rotateX", time=(1, 24))
cmds.keyTangent(itt="linear", ott="linear")
```

### 6. 渲染（Arnold）

```python
cmds.setAttr("defaultRenderGlobals.ren", "arnold")
cmds.setAttr("defaultArnoldRenderOptions.samples", 3)   # AA 采样
cmds.setAttr("defaultArnoldRenderOptions.AASamples", 3)
cmds.setAttr("defaultRenderGlobals.imageFilePrefix", "//render/hero_")
cmds.setAttr("defaultRenderGlobals.animation", 1)       # 渲染序列
cmds.setAttr("defaultRenderGlobals.endFrame", 48)
cmds.render()
```

### 7. FBX 导出（Unity/Unreal 管线——朝向/单位是重灾区）

```python
cmds.select(cube, replace=True)
cmds.file("//out/hero.fbx", force=True, options="v=0;", type="FBX export",
          pr=True, es=True)   # es=True：只导出选中
# Unreal 需注意：Maya Z-up → Unreal Z-up 一致；Unity Y-up 需在 FBX 导入设置转
# 或导出时开 "Convert to Y-up"（FBX 导出器选项 UpAxis=Y）
```

## 反模式清单（生成后必须自查）

| 反模式 | 后果 | 修正 |
|---|---|---|
| 命名空间/名称含中文空格 | 引擎/代码引用失败 | `[a-zA-Z0-9_]` + 前缀（hero_/env_/prop_） |
| 建模历史未清理（construction history） | 文件膨胀、导出异常、FBX 体积翻倍 | `cmds.delete(ch=True)`（清历史） |
| 单位不是 cm / 帧率混乱 | 导入引擎尺寸/动画节奏全错 | 开始前 `currentUnit` 定死 |
| FBX 导出不带 selected | 全场景导出（灯光/参考全带出） | `es=True` 或 `pr=True` + 明确选项 |
| 绑定权重未刷（默认均分） | 动画变形错误 | skinCluster 后用 `cmds.skinPercent` 检查/权重绘制 |
| 动画曲线默认 tangent | 循环动画接缝跳变 | keyTangent 线性化 + 循环首尾帧对齐 |
| 忘记 `cmds.select(clear=True)` 后建 joint | joint 层级被错误父子化 | 每段操作前明确选择状态 |
| 纹理路径绝对化 | 换机/打包后贴图丢失 | 用相对路径 + `filePathEditor` 重映射 |

## 验证步骤（脚本跑完必做）

1. mayapy/Script Editor 运行无报错
2. 大纲视图（Outliner）检查：命名规范、层级正确、无多余组
3. 视口检查：模型无破面、材质有颜色、绑定权重变形自然、动画流畅无跳变
4. 导出文件存在 + 导入引擎测试：Unity 检查朝向/缩放/材质、Unreal 检查比例/碰撞
5. 有 `blender_verify` 类工具时配合截图确认（用户规则：搞完 3D 相关改动实地查看）

## 常用资源

- Python：`maya.cmds`（命令）/ `pymel.core`（面向对象封装，推荐复杂逻辑）
- MEL：与 cmds 一一对应（`polyCube` ↔ `cmds.polyCube`）
- 导出：FBX（游戏通用）/ OBJ（通用网格）/ ABC（Alembic 缓存动画/解算）
- 渲染：Arnold（`aiStandardSurface`）/ 视口 Playblast（`cmds.playblast` 快速预览）
- 批处理：`mayapy -c "import maya.standalone; maya.standalone.initialize(); ..."`
