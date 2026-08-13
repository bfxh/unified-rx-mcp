# unified-rx 感知域修订版（前沿 + 实用，含诚实缺点清单）

> 2026-08-12 v2 · 回应"我要的是前沿加实用，你这些应该有好多的缺点吧"
> 上一版（PERCEPTION_PLAN.md）只列强项没列缺点——这版**每个候选先审缺点**，
> 按"前沿性 × 实用性 × 接入成本"打分，只留前沿且实用的组合。

---

## 第一部分：诚实缺点审查（上一版候选的真实缺点）

### 模型/视觉类

| 候选 | 前沿性 | 缺点（诚实版） | 结论 |
|---|---|---|---|
| **LLaVA（2023）** | ❌ 过时 | ① 2023 年架构，已被 Qwen-VL/MiniCPM-V 全面超越 ② 需 GPU ③ 对游戏截图无专门训练 ④ 微调要数据 | **弃**（换 Qwen3-VL） |
| **Pix2Struct（2022）** | ❌ 过时 | ① 2022 年模型，UI 理解已被现代 VLM 碾压 ② 微调需网页 HTML 数据 ③ onnx 生态差 ④ 游戏自绘 UI（无 DOM）适配存疑 | **弃**（UI 理解并入 VLM） |
| **SAM（2023）** | ❌ 过时 | ① 已被 SAM2 超越 ② 单点分割需提示工程 ③ GPU 重 ④ 对低多边形/像素风游戏分割质量差 | **换 SAM2**（仍不优先） |
| **CLIP（2021）** | ❌ 过时 | ① 2021 年模型，图文对齐已过时 ② 对程序化纹理/低多边形资产理解弱 ③ 中文弱 ④ 无定位能力 | **降级**（仅做相似度兜底） |
| **GroundingDINO（2023）** | ❌ 过时 | ① 已被 GroundingDINO 1.5/现代检测超越 ② 需要 GPU ③ 游戏物体无预训练 | 降级 |
| **whisper（原版）** | ⚠️ 可用 | ① 慢（原版 CTranslate2 快 4 倍）② 无流式 ③ 大模型 1.5GB | **换 faster-whisper** |
| **YOLO/ultralytics** | ✅ 前沿 | ① 需标注数据训练游戏物体 ② 60k★ 但游戏领域无预训练 | 保留（P2） |
| **3DGS（gaussian-splatting）** | ✅ 前沿 | ① GPU 重（训练 30min+）② 游戏资产重建不实用（你已有 GLB）③ 场景已由引擎渲染 | **弃**（游戏场景不需要重建） |

### 工具/库类

| 候选 | 前沿性 | 缺点（诚实版） | 结论 |
|---|---|---|---|
| **gltf-rs** | ✅ 标准 | ① 638★ 小生态 ② 纯解析不校验 ③ Rust 接入要写胶水 | **保留**（标准无可替代） |
| **trimesh** | ✅ 实用 | ① Python 重依赖（numpy 等）② 对 Bevy 资产需转换 ③ 大网格慢 | 保留（P0 网格校验） |
| **Open3D** | ✅ | ① 13.9k★ 但 1GB+ 依赖 ② 点云路对游戏资产不必要 | 降级（P2 才考虑） |
| **pytorch3d** | ✅ | ① GPU ② 训练路，非工具路 | 降级 |
| **playwright/appium/selenium** | ✅ 实用 | ① **网页/移动 DOM 专用——游戏自绘 UI 无 DOM 根本不适用**（上一版没写透）② 接入游戏=零价值 | **弃**（只抄断言哲学） |
| **bevy_mod_picking** | ⚠️ | ① 842★，2025-03 后停更 ② 与 Bevy 0.18 兼容性存疑 ③ 是交互插件非感知工具 | 参考射线思路即可 |

### 上一版方案的整体缺点（最重要的诚实）

1. **模型路线全都要 GPU**——你的主力环境（VoxelForge 开发机）无 GPU 加速路径
2. **Pix2Struct/LLaVA/SAM 全过时**——2022-2023 的模型，2026 年不该选
3. **没有"实用优先"分级**——把 P2 的 GPU 大模型和 P0 的纯函数混在一起
4. **playwright 等网页工具对游戏是伪需求**——游戏 UI 无 DOM，抄不了
5. **没考虑本地推理新范式**——llamafile/burn（Rust 原生推理）才是你的栈该用的

---

## 第二部分：2026 前沿替代（实查）

| 技术 | ★ | 为什么前沿 | 为什么实用 |
|---|---|---|---|
| **Qwen3-VL** | 19.8k | 2025-2026 视觉语言 SOTA（开放权重最强）| 2B/4B 小模型可本地，UI/截图理解直接可用 |
| **MiniCPM-V** | 26.2k | 端侧多模态（手机级可跑）| **8B 以下 GPU 可跑**，中文强，OCR 内置 |
| **SAM2** | 19.7k | 2024-2025 分割 SOTA（视频+图像）| 比 SAM 快 6×，提示灵活 |
| **faster-whisper** | 24.9k | CTranslate2 加速版 | 比原版快 4×，CPU 可跑，流式 |
| **llamafile** | 25.5k | 单文件 LLM（Mozilla）| **零依赖单 exe**——与 unified-rx 单二进制理念一致 |
| **burn** | 15.7k | **Rust 原生深度学习框架** | 你的栈是 Rust——不用 Python 胶水 |
| **candle** | 20.9k | HuggingFace Rust 推理 | 轻量、CPU 友好 |
| **MiniCPM-V / Qwen2.5-VL onnx** | - | 端侧部署 | onnxruntime 已有（local_intel 直接复用） |

---

## 第三部分：修订版方案（前沿 + 实用）

### 原则
1. **纯函数优先**（P0：零依赖、对 VoxelForge 立刻有用）
2. **模型路只选 2025+ 前沿 + 能本地跑**（Qwen3-VL 2B/MiniCPM-V/faster-whisper）
3. **Rust 原生优先**（burn/candle/llamafile——不搞 Python 胶水）
4. **弃过时模型**（LLaVA/Pix2Struct/SAM/CLIP 原版全弃）

### 修订路线

| Phase | 内容 | 前沿性 | 实用性 |
|---|---|---|---|
| **P0（纯函数，先做）** | `glb_info`（GLB 元数据）+ `mesh_check`（网格质量）+ `ui_tree_dump`（UI 树断言）+ `collider_check` | ✅ 工具标准 | ✅ 零依赖，VoxelForge 立即用 |
| **P1（本地模型路）** | `screenshot_ocr`（RapidOCR/PaddleOCR）+ `screenshot_describe`（**Qwen3-VL 2B** 或 MiniCPM-V 4B，onnx/llamafile）+ `clip_similarity`（BGE/CLIP 兜底） | ✅ 2025+ | ✅ CPU/小 GPU 可跑 |
| **P2（进阶）** | `scene_qa`（3D 场景问答：Qwen3-VL + 多视图渲染）+ `video_understand`（SAM2）+ `voice_cmd`（faster-whisper 流式） | ✅ 前沿 | ⚠️ 需 GPU |
| **P3（Rust 原生）** | **burn/candle 蒸馏**——把 Qwen3-VL 蒸馏成 Rust 原生小模型（unsloth 训练 → burn 部署）| ✅ 最前沿 | ✅ 单二进制，无 Python |

### 关键替换决策（诚实版）

| 原方案 | 改为 | 理由 |
|---|---|---|
| LLaVA 画面描述 | **Qwen3-VL 2B** | 2026 SOTA 开放权重，2B 本地可跑 |
| Pix2Struct UI 解析 | **并入 Qwen3-VL**（直接问"截图里 UI 有什么问题"）| 现代 VLM 已覆盖 UI 理解，不需要专门模型 |
| SAM 分割 | SAM2（P2）| 快 6×，视频可用 |
| whisper | **faster-whisper** | 快 4×，CPU 可跑 |
| playwright 抄断言 | **保留断言哲学**（text_visible/bounds_contained）但**不接 playwright** | 游戏无 DOM |
| Python 模型推理 | **burn/candle/llamafile**（Rust 原生）| 你的栈是 Rust |

---

## 第四部分：对 VoxelForge-Nexus 的实用组合（最优先）

```
今天就能用（P0）：
  glb_info      → 校验 release/assets/models/*.glb（顶点数/材质/尺寸单位——GLB 导出即校验）
  mesh_check    → 检测退化三角/非流形（贴图闪烁/破面根因）
  ui_tree_dump  → 导出 UI 节点树断言（布局越界/文本重叠——ui_layout_sync 的运行时验证）
  collider_check → 模块 def 的 ColliderSpec 与视觉尺寸匹配（装配手感 bug 根因）

下一步（P1）：
  screenshot_describe → 对 docs/game_render.png 直接问"画面有什么问题"（Qwen3-VL 2B）

远期（P2/P3）：
  scene_qa / voice_cmd / Rust 原生蒸馏
```

---

## 一句话

> **上一版最大的缺点：模型路线全过时（2022-2023）+ 全要 GPU + 混了伪需求（playwright）。**
> 修订版：纯函数先做（P0 零依赖）、模型路只选 2025+ 且本地可跑（Qwen3-VL 2B/MiniCPM-V/faster-whisper）、
> 最终走 Rust 原生推理（burn/candle/llamafile）——前沿且实用，不搞过时模型。
