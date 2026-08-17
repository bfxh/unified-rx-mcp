# media_check 剪辑/动画检查工具族（MEDIA_TOOLS）

> 2026-08-17 · 用户需求："IDE 对剪辑和动画的提升"；"看看有没有相关的工具 Rust 的，
> 如果没有就自己造一个"；渲染模拟用**完整渲染验证**。
> 文档先行（用户工作流：先方案后代码）。

## 一、定位

| 工具/模块 | 角色 | 说明 |
|---|---|---|
| **rx-media**（Rust crate） | 视频容器解析底层 | 零依赖（仅 serde_json，对齐 rx-core）手写 MP4/MOV box 解析：ftyp/moov/mvhd/tkhd/mdhd/hdlr/stsd/stts → 时长/分辨率/帧率估算/编码/轨道/损坏检测；`rx-media info <file>` + stdin 常驻模式 |
| **media_core.py** | 剪辑/动画检查核心 | video_info（Rust 优先 + Python atom 解析降级）/ timeline_check（Blender VSE）/ anim_check（.blend + .glb）/ render_sim（完整渲染验证） |
| **media_scripts/** | Blender bpy 批处理 | vse_check.py / anim_check.py / render_sim.py（`blender -b <blend> -P script.py -- args`） |
| **media_check**（MCP 工具） | 统一入口 | action=video/timeline/anim/render |
| **layer_check**（扩展） | 分层理念 | clip（粗剪→精剪→调色音效）/ anim3d（建模绑定→K帧→渲染）模板 |

## 二、各动作检测规则

### video（视频容器信息）
- 引擎链：rx-media（Rust，30s 超时）→ 失败降级纯 Python atom 解析（同字段 parity）
- 字段：brand/compatible/timescale/duration_sec/tracks（id/kind/width/height/codec/samples/fps_est）/has_video/has_audio/damaged
- 损坏检测：非 MP4 魔数/文件过短/moov 缺失/duration=0/无轨道

### timeline（Blender VSE 时间线）
- 素材断链：MOVIE/IMAGE strip filepath 不存在
- 时长越界：strip frame_start + duration > scene.frame_end
- 帧率混用：strip fps_source 来源不一致
- 分辨率：scene.render.resolution 报告

### anim（动画检查）
- .blend（bpy）：action/fcurves/keyframe_points、骨骼（armature/bones）、蒙皮（ARMATURE modifier）、驱动器；空 action/有网格无骨架/有骨架无蒙皮检测
- .glb/.gltf：animations（channels/samplers 引用越界、空动画）+ skins（joints 越界/缺失）——落地 PERCEPTION_PLAN_v3 的 glb_info 概念

### render（完整渲染验证——用户选定）
- `blender -b` 批处理渲染：frames=ALL（全帧动画）/范围（1-10）/单帧
- engine 归一化：CYCLES / EEVEE→BLENDER_EEVEE / WORKBENCH→BLENDER_WORKBENCH（Blender 5.x）
- 临时输出目录渲染 PNG → 校验输出文件齐全且非空；恢复场景设置

## 三、降级链（graceful）

| 依赖 | 缺失时 |
|---|---|
| rx-media 未编译 | video_info 自动用纯 Python atom 解析（同字段） |
| Blender 不可用（BLENDER_EXE > local-tools 注册表 > D:\rj\GJ\Blender 5.2） | timeline/anim/render 返回明确 error + 降级指引（video_info 逐素材检查） |
| 非 .blend/.glb 文件 | 明确提示支持格式 |

## 四、分层理念映射

| 领域 | 第一层 | 第二层 | 第三层 | 每层验证工具 |
|---|---|---|---|---|
| 剪辑 clip | 粗剪（素材齐/顺序对） | 精剪（转场/节奏） | 调色音效（色彩/音频/字幕） | video_info / timeline_check |
| 3D 动画 anim3d | 建模绑定（网格/骨骼） | K帧（关键帧/动画） | 渲染（材质/灯光/输出） | anim_check / render_sim |
| UI（已有） | 布局 | 动画 | 美术 | layer_check ui |

顺序违规校验：下层完成但上层未完成 → violations 提示（沿用"每层验证通过才进下一层"理念）。

## 五、验收

1. `cargo check`（rx-media）零警告；合成 MP4 全字段解析正确；损坏检测正确
2. `pytest test_media_core.py` 9/9（含 Blender 真实批处理）
3. `pytest test_layer_check.py` 11/11（clip/anim3d 模板 + 顺序违规）
4. Blender 5.2 实跑：vse_check（空场景 JSON）/ anim_check（默认 cube 网格无骨架提示）/ render_sim（1 帧 PNG 输出齐全）
5. tools.json 一致性（81 核心）
