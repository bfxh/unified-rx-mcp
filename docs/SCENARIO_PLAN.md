# unified-rx 场景驱动方案（按特殊场景配技术）

> 2026-08-12 · 基于全域调研（RESEARCH_GLOBAL.md）+ 用户项目实勘
> 核心原则：**技术不是越多越好，是每个特殊场景配它最强的组合**。
> 场景依据：D:\开发 实勘（Rust 引擎 ×5、Godot 游戏 ×8、MCP/知识项目 ×N）。

---

## 〇、你的场景地图（实勘）

| 场景 | 项目 | 规模 | 技术特点 |
|---|---|---|---|
| **S1 Rust 引擎开发** | VoxelForge-Nexus / biomorph / biomorph-physics / ar3d / wasteland_project | 大 | 多 crate、复杂调用图、性能敏感 |
| **S2 Godot 游戏开发** | LostCastle2 / nature_demo / nailong-cell / AAA_Game / VoxelForge / mechworld_v2 等 8+ | 中 | GDScript+场景树、美术资产、UI 多 |
| **S3 AI 编码辅助** | Reasonix 会话（unified-rx 日常被调） | — | 上下文掌握、防幻觉、反馈 |
| **S4 知识炼化** | AI知识库 / 炼化知识库 / kb_fenxiang | 文档多 | 中文内容、跨文档关联 |
| **S5 多项目巡航** | daemon 全盘扫（30+ 项目） | 大 | 高频扫描、资源受限 |
| **S6 安全质量门禁** | sec-workflows（已有 7 CI） | 中 | 漏洞/密钥/规范 |

---

## 一、S1｜Rust 引擎开发（VoxelForge-Nexus 等）

### 痛点
- 多 crate 调用关系复杂，AI 不知道"改这个函数影响谁"
- 引擎代码量大，grep/read 探索烧 token
- 性能代码 bug（unsafe/生命周期）普通扫描发现不了

### 配装（抄什么）
| 技术 | 来源 | 为什么配这里 |
|---|---|---|
| **知识图谱索引** | codebase-memory（★38.6k） | 调用图/影响分析 = 改 Rust 引擎的刚需；**已有 exe 在 D:\开发** |
| **rust-analyzer LSP** | cae_lsp_query 后端 | 类型/引用/跳转，Rust 语义最强 |
| **ast-grep 结构搜索** | ast-grep（★15.5k） | 引擎模式改写（如所有 `Vec::new()` 改 `with_capacity`） |
| **cargo 生态挂载** | cargo check/clippy | 编译级验证（比静态扫描准） |

### unified-rx 落地
- `cb_index` 升级：tree-sitter Rust 图索引 + **调用图影响分析**（`change_impact` 图论版）
- `cae_lsp_query` 接 rust-analyzer，三步走规则（改前 context → 改后 impact → 引用前 verify）
- `locate_edit` 加 ast-grep 模式后端
- 新工具 `repo_wiki`：引擎模块地图（crate → 模块 → 入口 → 依赖）

### 验收
- 对 VoxelForge-Nexus 实测：`change_impact("physics.rs 某函数")` 返回完整调用链
- token 消耗对比：图查询 vs 现状 grep 探索

---

## 二、S2｜Godot 游戏开发（8+ 项目）

### 痛点
- GDScript 语义弱（无 LSP 好后端）、场景树/节点关系 AI 看不懂
- UI 检查（已有 ui_check）只覆盖 Bevy，Godot 场景没覆盖
- 美术资产/导入管线（texture/audio）无感知

### 配装
| 技术 | 来源 | 为什么配这里 |
|---|---|---|
| **Godot LSP / 场景解析** | gdscript LSP + 场景树解析 | GDScript 符号+`.tscn` 节点图 |
| **meow-godot-mcp 桥** | 已有项目（D:\开发） | Godot 引擎直连（运行/调试/节点树） |
| **tree-sitter gdscript** | tree-sitter 语法库 | 代码结构索引（cb_index 扩展） |
| **CLIP 图像语义** | CLIP（★34.2k） | 资产理解（贴图内容→用途），可选 |

### unified-rx 落地
- `cb_index` 加 GDScript 语言（tree-sitter gdscript）
- `ui_check` 加 Godot 场景检查（.tscn 硬编码/节点命名）
- vendor 扩展接 meow-godot-mcp（引擎会话桥）
- `repo_wiki` 输出场景树地图（场景 → 节点 → 脚本 → 信号）

### 验收
- 对 LostCastle2 实测：`ui_check(path)` 扫出 Godot 场景问题
- `repo_wiki("LostCastle2")` 生成场景结构文档

---

## 三、S3｜AI 编码辅助（Reasonix 日常）

### 痛点
- 0 调用工具（lsp_query/code_context/change_impact）——能力存在没接线
- AI 上下文掌握靠多次 fs_read，token 烧得多
- 反馈是纯文本，不直观

### 配装
| 技术 | 来源 | 为什么配这里 |
|---|---|---|
| **AetherStudio LSP 三件套** | semantic_tokens/incremental_sync/FastLineIndex | 语义 token + 增量同步 = 上下文高效 |
| **AiRole::Tool 卡片** | AetherStudio 卡片形态 | 工具结果可视化（图标+状态） |
| **pipeline 配方** | 已有 | 三步走固化成 preset |

### unified-rx 落地
- cae lsp 三件套接线（0 调用 → 工作流核心）
- `tool_card` 卡片升级（op_kind/status/思考分离）
- REASONIX.md 规则：改前 code_context → 改后 change_impact → 引用前 lsp_query
- preset 固化："改代码"配方 = 定位→上下文→修改→验证→影响

### 验收
- 日常会话中 0 调用工具变高频
- mcp_smoke + selftest 通过

---

## 四、S4｜知识炼化（AI知识库 / 炼化知识库）

### 痛点
- 中文文档多、跨文档关联弱
- 检索靠文件名，语义检索没有
- 你"炼化"大量文档（27+ 项目 → 完整版 md），工具只存不检索

### 配装
| 技术 | 来源 | 为什么配这里 |
|---|---|---|
| **本地嵌入（BGE/text2vec）** | ★12k/5k | 中文语义检索，离线 |
| **RAG（LightRAG 轻量）** | ★38.8k（EMNLP'25） | 双级检索：具体/通用 |
| **知识图谱记忆** | cognee/GraphRAG（★30k/35k） | 文档关联（项目→技术→工具） |
| **蒸馏小模型** | unsloth（★70k）+ onnxruntime | 本地语义分类/摘要，资源小 |

### unified-rx 落地
- 新工具 `kb_query`：知识库语义检索（嵌入+重排）
- `lesson_recall_lse` 升级：嵌入检索 + 图谱关联 + 自动提取（抄 mem0）
- LSE 教训与知识库打通（教训 → 知识条目）
- 可选：蒸馏一个"代码注释语义"小模型（本地推理）

### 验收
- 对 AI知识库实测：`kb_query("MCP 架构模式")` 返回跨文档相关章节
- 教训自动提取：会话后自动入库

---

## 五、S5｜多项目巡航（daemon 全盘扫）

### 痛点
- 30+ 项目高频扫描，资源敏感（用户抱怨过内存/CPU）
- 扫描结果 JSONL 膨胀，查历史慢
- 重复扫描（缓存命中率低）

### 配装
| 技术 | 来源 | 为什么配这里 |
|---|---|---|
| **热/温/冷存储** | AetherStudio ai_hot_data | mmap 热数据 + SQLite 归档 + 压缩 |
| **全文索引（tantivy/FTS5）** | ★15.7k | scan-log 秒查 |
| **缓存升级** | scan_cache 已有 | mtime+内容 hash 双键 |

### unified-rx 落地
- scan-log 升级三层存储（防膨胀）
- `scan_log` 查询走全文索引
- daemon 间隔自适应（项目活跃度高→高频）

### 验收
- 1000 条日志查询 <100ms
- 内存占用对比（三层 vs JSONL）

---

## 六、S6｜安全质量门禁

### 配装
| 技术 | 来源 | 为什么配这里 |
|---|---|---|
| ruff（★49k） | Rust lint | Python 快 100× |
| semgrep（★16k） | 模式规则 | 比手写 AST 简洁 |
| gitleaks（★28.6k） | 密钥检测 | 防泄漏 |
| pyright（★15.6k） | 类型检查 | 静态类型 |

### unified-rx 落地
- `std_check` 规则引擎换 ruff（Python 域）
- `bug_scan` 加 semgrep 模式集
- 新工具 `secret_scan`（gitleaks 规则）
- 接入 sec-workflows CI

---

## 七、优先级总表（场景 × 阶段）

| 阶段 | 场景 | 动作 | 投入 |
|---|---|---|---|
| **P0（先做）** | S3 AI 编码 | cae lsp 三件套接线 + 三步走规则 | 小 |
| **P0** | S1 Rust 引擎 | cb_index 图索引 + change_impact 调用链 | 中 |
| **P1** | S4 知识炼化 | 本地嵌入 + kb_query + LSE 升级 | 中 |
| **P1** | S2 Godot | GDScript 索引 + ui_check 扩展 | 中 |
| **P2** | S5 巡航 | 三层存储 + 全文索引 | 小 |
| **P2** | S6 门禁 | ruff/semgrep/gitleaks 接入 | 小 |

**逻辑**：S3 是日常（立刻见效）→ S1 是主业（Rust 引擎）→ S4 是知识资产（你的炼化）→ S2 游戏 → S5/S6 基建。

---

## 八、一句话总结

> **全域技术是弹药库，场景是战场：Rust 引擎战场配知识图谱+LSP（S1）、Godot 战场配场景树+引擎桥（S2）、日常编码战场配 LSP 三件套+卡片（S3）、知识战场配本地嵌入+RAG+蒸馏（S4）、巡航战场配三层存储（S5）、门禁战场配 ruff/semgrep（S6）。每个场景的技术都从调研里挑最强的，接入点都是 unified-rx 现有工具——旧代码全是地基。**
