# unified-rx 全域技术调研（四大域 × 对比矩阵 × 旧特性复用）

> 2026-08-12 · 全域扫描：语义 / 学习与蒸馏 / Agent 工具箱 / 旧特性复用
> 目标：**全方面强**——每样技术都对比、对比完看 unified-rx 旧特性怎么用上它。
> 数据源：GitHub API 实测（★为当日值）+ arXiv + 官方文档。

---

## 域 1：语义全家桶（不只是 IDE 语义）

### 1.1 代码语义（结构/符号/类型）

| 技术 | 项目 | ★ | 核心能力 | 与 unified-rx 关系 |
|---|---|---|---|---|
| 知识图谱代码索引 | codebase-memory-mcp | 38.6k | tree-sitter 66 语言→知识图谱，83%质量/10×少token | **cb_index 终极形态**（论文 2603.27277） |
| LSP 语义 | AetherStudio 三件套 | 91 | semantic tokens/增量同步/FastLineIndex | cae_lsp_query 升级模板 |
| 结构搜索 | ast-grep | 15.5k | 代码模式搜索/改写 | locate_edit 后端 |
| 语义索引协议 | SCIP | 723 | 代码索引互操作标准 | cb_index 导出格式参考 |
| 标签索引 | universal-ctags | 7.3k | 符号标签（66 语言） | cb_index 轻量备选 |
| 类型检查 | pyright | 15.6k | 静态类型（Python） | std_check 加类型路 |

### 1.2 自然语言语义（嵌入/检索/RAG）

| 技术 | 项目 | ★ | 核心能力 | 用法 |
|---|---|---|---|---|
| 通用嵌入 | sentence-transformers | 19k | 文本向量化（多语言） | lesson_recall 语义检索 |
| 中文嵌入 | BGE / text2vec | 12k/5k | 中文 SOTA 嵌入 | 中文代码注释/文档检索 |
| RAG 引擎 | RAGFlow | 87.3k | 深度 RAG（解析+分块+混合检索） | 知识库问答 |
| 图 RAG | GraphRAG | 35.4k | 图结构检索 | **LSE 教训图谱化** |
| 轻量 RAG | LightRAG | 38.8k | 双级检索，EMNLP'25 | 快速检索层 |
| 向量库 | qdrant/milvus/faiss | 34k/46k/41k | 向量存储检索 | cb_index 语义层 |
| 全文索引 | tantivy | 15.7k | Rust 全文检索 | cb_index 全文层 |
| Rerank | BCEmbedding | 1.9k | 重排精排 | 检索后精排 |

### 1.3 其他语义（图像/音频/多模态）——工具箱可选扩展

| 技术 | 项目 | ★ | 能力 |
|---|---|---|---|
| 图文语义 | CLIP | 34.2k | 图文对比学习 |
| 语音 | whisper | 107k | 语音转文字 |
| OCR | RapidOCR | — | 本地 OCR（用户已有 ocr_mcp_server.py） |
| 多模态 VLM | mlx-vlm | 5.3k | 本地 VLM 推理 |

---

## 域 2：学习与蒸馏（模型级技术）

### 2.1 模型压缩/蒸馏

| 技术 | 项目 | ★ | 核心 | 适合 unified-rx 吗 |
|---|---|---|---|---|
| 知识蒸馏 | distil-whisper | 4.1k | 教师→学生，6×快/50%小/1%误差 | ✅ 蒸馏小模型做本地语义 |
| 量化 | intel/neural-compressor | 2.7k | INT8/FP8/INT4 量化+稀疏 | ✅ 本地嵌入/分类模型瘦身 |
| 推理加速 | onnxruntime | 21.4k | 跨平台推理 | ✅ 嵌入模型部署 |
| 低比特推理 | llama.cpp | 123.6k | GGUF 量化 | ✅ 本地小模型 |
| 蒸馏实现 | optimum | 3.5k | transformers 蒸馏工具箱 | ✅ 蒸馏管线 |

### 2.2 训练/微调

| 技术 | 项目 | ★ | 核心 | 用法 |
|---|---|---|---|---|
| 参数高效微调 | PEFT/LoRA | 21.5k/13.7k | LoRA 低秩适配 | ✅ 微调本地语义模型 |
| RL 训练 | trl | 19k | RLHF/DPO/GRPO | ⚠️ LSE 引擎的 RL 侧参考 |
| 高效训练 | unsloth | 70.4k | 本地训练 2×快/省 70% 内存 | ✅ 蒸馏/微调落地 |
| 分布式 | DeepSpeed | 42.9k | 大规模训练 | ❌ 超需求 |
| 训练框架 | pytorch-lightning | 31.3k | 训练工程化 | ⚠️ 可选 |

### 2.3 自我进化（unified-rx 已入此域）

| 技术 | 论文 | 核心 | 状态 |
|---|---|---|---|
| **LSE** | arXiv:2603.18620 | 树引导进化+delta奖励 | ✅ lse-engine 已实现 |
| self-play | AlphaZero | 自我对弈进化 | ⚠️ bug_locate 可借鉴 |
| RLHF/DPO | trl | 人类反馈对齐 | ⚠️ 教训权重可类比 |

---

## 域 3：Agent 工具箱全集

### 3.1 编排框架

| 项目 | ★ | 核心 | 借鉴点 |
|---|---|---|---|
| langgraph | 39.5k | 图状态机 Agent | pipeline 的图升级 |
| autogen | 60.4k | 多 Agent 对话 | 多智能体协作 |
| crewAI | 57k | 角色编排 | 角色分工 |
| semantic-kernel | 28.4k | 企业 Agent | 插件模式 |
| openai-agents | 28.6k | 轻量多 Agent | 工具循环模式 |
| pydantic-ai | 19.2k | 类型安全 Agent | schema 自动生成 |
| Qwen-Agent | 17k | 函数调用+工具 | **MCP+工具融合参考** |
| FastChat | 39.5k | 训练/服务/评测 | 评测参考 |

### 3.2 记忆系统（对应⑤记忆掌握）

| 项目 | ★ | 核心 | 借鉴点 |
|---|---|---|---|
| mem0 | 63.1k | 通用 Agent 记忆层 | **自动记忆提取** |
| Letta | 24.2k | 分层记忆（MemGPT） | 核心/工作/存档三层 |
| cognee | 30k | 知识图谱记忆 | 教训图谱化 |
| siyuan | 45.8k | 知识工作空间 | 知识库整合 |

### 3.3 规划/推理/工具使用

| 技术 | 论文/项目 | 核心 |
|---|---|---|
| ToT | NeurIPS'23 | 思维树 |
| LATS | ICML'24 | MCTS×ReAct 统一 |
| AlphaCodium | 论文 | 流程工程（生成→测试→修正） |
| SWE-agent | NeurIPS'24 | 真实 issue 修复 |

---

## 域 4：旧特性复用映射（unified-rx 59 工具怎么用上新技术）

| 现有工具 | 现在干什么 | 新技术 | 复用方式 |
|---|---|---|---|
| **cb_index** | AST 索引+变更感知 | codebase-memory 知识图谱 | 索引层换 tree-sitter 图，查询换图遍历 |
| **cae_lsp_query** | LSP 查询（0 调用） | AetherStudio 三件套 | 算法升级+接工作流三步走 |
| **locate_edit** | 自然语言→位置 | ast-grep 结构搜索 | 后端加结构模式匹配 |
| **lesson_recall_lse** | 教训召回 | mem0/Letta/cognee | 分层记忆+自动提取+图谱化 |
| **bug_locate** | UCB 树搜索 | LATS/ToT | 值函数引导+多轮探索 |
| **std_check** | 正则工程标准 | ruff/semgrep 规则库 | 规则引擎升级 |
| **bug_scan** | AST 缺陷扫描 | codeql 查询 | 语义查询替代手写遍历 |
| **tool_card** | AiRole::Tool 回喂 | AetherStudio 卡片形态 | op_kind/status 扩展 |
| **pipeline** | 步骤链配方 | langgraph | 图状态机升级 |
| **scan-log** | JSONL 落盘 | AetherStudio 热/温/冷 | mmap 热数据+SQLite 归档 |
| **fs_*** | 文件层 | ripgrep | 文本搜索加速 |
| **rx-core** | Rust 纯函数 | llama.cpp/onnxruntime | 本地嵌入/分类推理后端 |
| **LSE 引擎** | 教训进化 | trl/DPO | 权重更新对齐 RL 语义 |
| **daemon** | 7 循环扫描 | — | 挂载新引擎（图谱增量/蒸馏定时） |

---

## 五、对比矩阵（关键技术选型）

### 5.1 代码索引引擎对比

| 维度 | cb_index（现状） | ctags | tree-sitter+图（目标） | codebase-memory |
|---|---|---|---|---|
| 语言覆盖 | Python ast | 66 | 66 | 158 |
| 结构 | 线性索引 | 标签 | 图 | 图 |
| 查询 | 关键词 | 符号 | 图遍历 | 图遍历+子图 |
| 部署 | 内嵌 Python | 二进制 | 二进制/Rust | 单二进制 |
| token 效率 | 中 | 高 | 高 | **10× 省** |
| 成熟度 | 自研 | 稳定 | 需建 | **生产级** |

### 5.2 记忆系统对比

| 维度 | LSE（现状） | mem0 | Letta | cognee |
|---|---|---|---|---|
| 结构 | 平铺教训 | 实体-关系 | 三层 | 知识图谱 |
| 提取 | 手动反馈 | **自动** | 自动 | 自动 |
| 检索 | utility 排序 | 混合 | 分层 | 图遍历 |
| 进化 | ✅ delta+UCB | ❌ | ❌ | ❌ |
| 适合 | 教训 | 通用记忆 | 会话记忆 | 图谱记忆 |

### 5.3 蒸馏路线对比（本地语义引擎）

| 方案 | 成本 | 能力 | 适合 |
|---|---|---|---|
| 嵌入 API（外部） | 简单 | 强 | 快速起步 |
| BGE/text2vec 本地 | 低 | 中强 | **离线语义检索** |
| 蒸馏小模型（unsloth） | 中 | 定制 | 专用语义（代码注释） |
| llama.cpp 量化 | 低 | 通用 | 本地 LLM 兜底 |

---

## 六、落地路线（四域合并）

### Phase 1（语义+结构）
- cb_index → tree-sitter 知识图谱（抄 codebase-memory 算法）
- repo_wiki 工具（抄 Qoder Repo Wiki 形态）
- cae lsp 三件套接线（抄 AetherStudio）

### Phase 2（检索+质量）
- 本地嵌入（BGE/text2vec + onnxruntime 或蒸馏）→ lesson 语义检索
- tantivy/SQLite FTS5 全文层
- ruff/semgrep/gitleaks 规则引擎

### Phase 3（记忆+进化）
- LSE 分层（抄 Letta）+ 自动提取（抄 mem0）+ 图谱化（抄 cognee）
- bug_locate → LATS 值函数引导

### Phase 4（Agent 协作+部署）
- pipeline → langgraph 式图编排
- 单二进制化评估（rx-core+lse+索引引擎）
- SWE-bench 子集评测

---

## 七、一句话总结

> **全域调研结论：unified-rx 要"全方面强"，不是加工具，是把七维掌握每条线都升级到业界最强——结构抄 codebase-memory（图索引）、语义抄 AetherStudio（LSP三件套）+本地嵌入、记忆抄 mem0/Letta/cognee（分层+图谱+自动提取）、探索抄 LATS（值函数树搜索）、质量抄 ruff/semgrep/codeql、蒸馏用 unsloth/onnxruntime 落地本地模型。每项新技术的接入点，就是现有 59 工具里的对应工具（见域4映射表）——旧代码全部是地基，不是屎山。**
