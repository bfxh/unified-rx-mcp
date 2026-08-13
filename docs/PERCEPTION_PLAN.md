# unified-rx 感知域方案（UI 检测 × 3D 感知 × 画面/音频/物理）

> 2026-08-12 · 全域调研：技术（GitHub 实测★）+ 论文（arXiv 实读）+ 场景驱动
> 定位：给 unified-rx 加"眼睛"——**感知**是掌握的第 8 维（结构/语义/定位/探索/记忆/反馈/质量 + **感知**）。
> 场景依据：VoxelForge-Nexus（Bevy 3D 游戏）+ Godot 游戏 + 知识库。
> 方法论：技术是抽象的、特点是具体的——每项列强项/弱项/接入点；场景配技术。

---

## 〇、感知域总览（工具箱的"眼睛"）

```
感知 = 让 AI 看到游戏/3D/UI 世界：
├── ① UI 感知    游戏 UI 树检查 / 截图断言 / 视觉 UI 理解（Pix2Struct）
├── ② 3D 感知    glTF 解析 / 网格质量 / 场景图查询 / 3D 视觉理解（3D-LLM）
├── ③ 画面感知   截图描述（LLaVA）/ 分割（SAM）/ 检测（GroundingDINO）
├── ④ 音频感知   语音转文字（whisper）/ 音频特征
├── ⑤ 物理感知   碰撞体一致性 / 刚体约束检查（parry）
└── ⑥ 嵌入感知   视觉嵌入检索（CLIP）/ 多模态向量（已有 kb_query 向量路）
```

---

## 域 1：UI 检测（先写这个——你点名）

### 现状：unified-rx 已有
- `ui_check`（Bevy UI 静态检查：ui_root_missing/camera_missing/mode_isolation/focus_pass/font_missing/z_ordering）
- `std_check` 的 UI 硬编码值检查
- 局限：**纯代码静态扫描**——看不到运行时的 UI 树和真实画面

### 技术对比（实测★）

| 技术 | ★ | 特点（强项） | 弱项 | 对 unified-rx 的接入点 |
|---|---|---|---|---|
| **Pix2Struct**（论文 ICML'23） | - | **截图解析为简化 HTML 预训练**，UI 理解 6/9 任务 SOTA | 需微调 | **ui_screenshot_parse**：截图→UI 结构描述（P1 视觉路） |
| **playwright** | 94.4k | 浏览器 UI 自动化测试事实标准（截图/断言/树） | 网页专用 | 参考其 **expect 断言模型**（locator 语义） |
| **appium/selenium** | 21.8k/34.3k | 移动/Web 端到端测试 | 游戏场景不适用 | 游戏 = 自绘 UI 无 DOM——**不能直接抄**，抄断言哲学 |
| **tauri** | 110.1k | Rust 桌面框架 | 不是测试工具 | Bevy 游戏 UI 同源（原生渲染无 DOM）——**参考其 webview 调试** |
| **bevy_ui_dsl / bevy-inspector-egui** | - | Bevy UI 树可视化/DSL | 编辑器工具 | **ui_tree_dump**：运行时导出 UI 节点树（bevy 反射） |

### UI 检测三层方案（从易到难）

```
L0 静态（已有 ui_check）→ L1 运行时树（新：ui_tree_dump——Bevy UI 节点树导出+断言）
→ L2 视觉（新：screenshot_parse——Pix2Struct 截图→UI 结构，检测文本重叠/越界/遮挡）
```

**L1 运行时 UI 树**（纯代码，先做）：
- 工具 `ui_tree_dump(root)`：解析 Bevy 场景/UI 代码 → 节点树 JSON（Node/Text/Button/层级/布局值）
- 断言模型（抄 playwright expect）：`text_visible / bounds_contained / z_order_ok / no_overlap`
- 对比 ui_layout 的布局值与窗口边界（防越界/重叠——游戏 UI 高频 bug）

**L2 视觉 UI 理解**（P1，模型路）：
- Pix2Struct 微调版（onnxruntime 可跑）→ 截图 → 简化 UI 结构 HTML
- 检测：文本截断/按钮重叠/不可见元素——**实机截图直接断言**（你的 game_render.png 就能测）

---

## 域 2：3D 感知（你点名"针对3D"）

### 技术对比

| 技术 | ★ | 特点（强项） | 弱项 | 接入点 |
|---|---|---|---|---|
| **gltf-rs** | 638 | Rust glTF/GLB 解析（场景/网格/材质/动画/骨架） | 纯解析 | **glb_info**：GLB 资产元数据（顶点/材质/动画/骨架——MODELING_EXPORT_GUIDE 的自动校验） |
| **trimesh** | 3.6k | Python 网格分析（非流形/退化三角/空洞/体积/质心） | Python | **mesh_check**：网格质量（退化三角形/非流形/比例异常） |
| **meshoptimizer** | - | 网格优化（简化/量化/LOD 生成） | C++ | 参考其 LOD 思路（nexus_physics 已有 lod） |
| **Open3D** | 13.9k | 3D 几何处理（点云/重建/可视化） | 重 | 点云感知路（P2） |
| **pytorch3d** | 9.9k | 3D 深度学习（可微渲染/网格 CNN） | GPU | 3D 视觉模型训练（P2） |
| **3DGS（gaussian-splatting）** | 22.9k | 3D 高斯泼溅（新视图合成 SOTA） | GPU | 场景重建（P3 远期） |
| **bevy_mod_picking** | 842 | Bevy 拾取（射线/悬停/点击） | Bevy 插件 | 复用其 **射线查询**做场景感知 |
| **parry** | 854 | Rust 碰撞检测（Bevy 物理底层） | 库 | **collider_check**：碰撞体与网格一致性 |

### 3D 感知四层方案

```
L0 资产元数据（先做）：glb_info——GLB 顶点数/材质/动画/骨架/尺寸（建模导出的自动校验）
L1 网格质量（先做）：mesh_check——退化三角/非流形/空洞/单位异常（贴图闪烁/破面的根因）
L2 场景图查询（P1）：scene_graph——Bevy ECS 场景导出（实体/组件/父子——"游戏世界长什么样"）
L3 3D 视觉理解（P2，模型路）：3D-LLM 思路——多视图渲染→3D 特征→问答（"这个模型是什么/空间关系"）
```

**L0/L1 是纯函数**（gltf-rs/trimesh 解析），对你的 GLB 管线（MODELING_EXPORT_GUIDE）直接有用：
- 建模导出后自动校验：顶点上限/材质命名/尺寸单位（米）/骨架命名——**导出即校验**

---

## 域 3：画面感知（游戏截图理解）

| 技术 | ★ | 特点 | 弱项 | 接入点 |
|---|---|---|---|---|
| **SAM（segment-anything）** | 54.7k | 分割一切（零样本） | GPU | screenshot_segment：画面物体分割（P2） |
| **GroundingDINO** | 10.5k | 文本→检测框（开集检测） | GPU | "找到红色的模块"→框（P2） |
| **CLIP** | 34.2k | 图文对齐（视觉嵌入） | 无定位 | **clip_similarity**：截图与描述的相似度（画面断言：P2） |
| **LLaVA**（NeurIPS'23 Oral） | 25.0k | 视觉指令微调（看图问答） | 重 | screenshot_describe：截图→自然语言描述（P2，Science QA 92.5% SOTA） |
| **ultralytics（YOLO）** | 60.6k | 实时检测（工业标准） | 需训练数据 | 游戏物体检测（P2） |
| **RapidOCR** | - | 本地 OCR（中文） | 准确性一般 | **screenshot_ocr**：截图文字提取（L1 先做——无 GPU 依赖） |

**画面感知分层**：L1 OCR（本地，先做）→ L2 CLIP 相似度（P1，onnx 可跑）→ L3 SAM/LLaVA（P2，GPU）

---

## 域 4：音频感知

| 技术 | ★ | 特点 | 弱项 | 接入点 |
|---|---|---|---|---|
| **whisper** | 107.1k | 语音转文字（SOTA） | 重 | 语音指令识别（P2） |
| **MeloTTS/parler-tts** | 7.6k/5.6k | 本地 TTS | 质量一般 | 游戏语音反馈（P3） |
| songsee（已有 skill） | - | 频谱/特征 | - | 音频特征提取（已有） |

---

## 域 5：物理感知（游戏专属）

- **collider_check**：碰撞体（Collider）与网格/模块定义一致性——模块 def 的 collider 是否与视觉尺寸匹配（VoxelForge 的 def 里 ColliderSpec 与模块形状）
- **weld_group_check**：焊接组刚体引用有效性（游戏已有 weld_groups——检查悬空引用）

---

## 域 6：嵌入感知（复用已有）

- **kb_query 向量路**（已有：BGE/CLIP 嵌入接口 + RRF）→ 加视觉嵌入（CLIP）→ **多模态检索**：截图搜知识库/知识库搜截图

---

## 论文清单（实读摘要）

| 论文 | 核心 | 对 unified-rx 的意义 |
|---|---|---|
| **Pix2Struct**（ICML'23, arXiv:2210.03347） | 截图→简化 HTML 预训练，UI 理解 6/9 SOTA | UI 检测 L2 的模型依据 |
| **3D-LLM**（arXiv:2307.12981） | 3D 点云注入 LLM：3D 问答/定位/导航，ScanQA BLEU-1 超 SOTA 9% | 3D 感知 L3 的模型依据 |
| **LLaVA**（NeurIPS'23 Oral, arXiv:2304.08485） | 视觉指令微调，Science QA 92.53% SOTA | 画面理解 L3 的模型依据 |
| **Codebase-Memory**（arXiv:2603.27277，已有） | tree-sitter 知识图谱 MCP | 感知结果入图谱（感知↔掌握闭环） |

---

## 落地路线（按场景重要性）

| Phase | 内容 | 验证 |
|---|---|---|
| **P0（先做，纯函数零依赖）** | `glb_info`（GLB 元数据）+ `mesh_check`（网格质量）+ `ui_tree_dump`（UI 树断言）+ `collider_check`（碰撞体一致性） | 对 VoxelForge 的 release GLB/UI 代码实测 |
| **P1（模型路 onnx 可跑）** | screenshot_ocr（RapidOCR）+ clip_similarity（CLIP onnx）+ Pix2Struct UI 解析 | 对 game_render.png 实测 |
| **P2（GPU 路）** | SAM/GroundingDINO/LLaVA 画面理解 + 3D-LLM 场景问答 | 截图+场景实测 |
| **P3（远期）** | 3DGS 场景重建 / TTS 语音反馈 | - |

## 旧特性复用映射（感知↔掌握闭环）

| 已有 | 感知新技术 | 怎么接 |
|---|---|---|
| graph_index（掌握） | glb_info/mesh_check | 感知结果作为节点入图（"资产节点"） |
| kb_query 向量路 | CLIP 视觉嵌入 | 多模态检索（截图↔代码↔文档） |
| local_intel（推理层） | CLIP/Pix2Struct onnx | 模型统一入口（已有降级机制） |
| storage_tiers | 截图/感知日志 | 热温冷存感知历史 |
| lesson_extract | 感知发现 | 自动提取"UI 越界/网格破面"教训 |
| ui_check（已有） | ui_tree_dump | 静态→运行时升级（同一断言语义） |

---

## 一句话

> **感知 = 工具箱的"眼睛"**：P0 先做纯函数检测（GLB/网格/UI 树/碰撞体——零依赖、对 VoxelForge 立刻有用），P1 上 onnx 模型路（OCR/CLIP/Pix2Struct），P2 上 GPU 大模型路（SAM/LLaVA/3D-LLM）。感知结果全部回流已有的掌握引擎（图/检索/记忆）——**看到的东西变成经验**。
