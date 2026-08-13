# unified-rx 全方位强化调研（技术 × 论文 × 项目 × 可抄清单）

> 2026-08-12 · 实勘 GitHub 高星项目 + arXiv 论文 + AetherStudio/Qoder 原理逆向
> 目标：**全方面强**——每个"掌握"维度列出业界最强方案、论文依据、可抄点、接入方式。
> 原则：不造轮子，能抄就抄，抄完统一进 unified-rx 的七维掌握框架。

---

## 〇、总览：七维掌握 + 三大支撑

unified-rx 的强化地图（每格 = 可抄的业界最强方案）：

| 维度 | 现状 | 业界最强（可抄） | 论文依据 | 优先级 |
|---|---|---|---|---|
| ① 结构掌握 | cb_index（AST+变更感知） | **codebase-memory-mcp**（tree-sitter 知识图谱）、SCIP、ctags | arXiv:2603.27277 | 🔴 P0 |
| ② 语义掌握 | cae_lsp_query（0 调用） | **AetherStudio 三件套**（semantic_tokens/incremental_sync/FastLineIndex）、pyright | LSP 标准 | 🔴 P0 |
| ③ 定位掌握 | locate_edit（Qoder 式） | **ast-grep**（结构搜索/改写）、Qoder Repo Wiki | — | 🟠 P1 |
| ④ 探索掌握 | bug_locate UCB（LSE） | **LATS**（语言Agent树搜索）、ToT、LSE 树引导进化 | ICML 2024 / NeurIPS 2023 / arXiv:2603.18620 | 🟠 P1 |
| ⑤ 记忆掌握 | lesson_recall_lse | **mem0 / Letta / cognee**（知识图谱记忆）、LightRAG/GraphRAG | EMNLP 2025 / arXiv:2603.18620 | 🟠 P1 |
| ⑥ 反馈掌握 | tool_card（AiRole::Tool） | **AetherStudio 卡片形态**（图标+标签+主题色） | — | 🟢 P2 |
| ⑦ 质量掌握 | std_check/bug_scan | **ruff / semgrep / codeql / pyright / bandit / gitleaks** | — | 🟠 P1 |
| 支撑1 搜索 | scan_cache LRU | **ripgrep / tantivy / meilisearch**（全文索引） | — | 🟠 P1 |
| 支撑2 性能 | rx-core Rust 常驻 | 单二进制部署（codebase-memory 模式）、uv | — | 🟢 P2 |
| 支撑3 评测 | pytest/parity/ratchet | **SWE-bench / AlphaCodium 流程**、evals | NeurIPS 2024 | 🟢 P2 |

---

## 一、🔴 结构掌握（P0）—— 抄 codebase-memory-mcp

### 项目：codebase-memory-mcp ★38.6k（用户已下载在 D:\开发）
**论文：arXiv:2603.27277**（Codebase-Memory: Tree-Sitter-Based Knowledge Graphs for LLM Code Exploration via MCP）
- 摘要要点：31 个真实仓库评测，**83% 答案质量（vs 文件探索 agent 92%），但 token 少 10×、工具调用少 2.1×**；图原生查询（hub 检测/调用者排序）19/31 语言持平或超越
- 核心管线：tree-sitter 解析 66 语言 → 并行 worker 池 → **调用图遍历 + 影响分析 + 社区发现** → 持久化知识图谱

### 可抄点（按顺序）
1. **cb_index 升级为知识图谱**：AST 解析 → 节点（函数/类/文件）+ 边（调用/导入/继承）→ 图查询（谁调用我/我调用谁/影响面）
2. **tree-sitter 替代手写 AST**：Python ast 只覆盖 Python；tree-sitter 66 语言（Python 版 py-tree-sitter 可嵌入）
3. **impact analysis 算法**：调用图反向遍历 = 你的 `change_impact` 的图论版
4. **hub 检测 + 调用者排序**：图中心性 = 新工具 `repo_hub`（找核心模块/上帝文件）
5. **持久化格式**：SQLite（内存优先 + LZ4 压缩）——比 `.unified-rx-index/index.json` 强

### 接入方式
- 独立扩展（如 vendor/codebase-memory/），二进制直接调用（用户已有 exe！）
- 或先抄算法进 cb_index_core.py，逐步替换

---

## 二、🔴 语义掌握（P0）—— 抄 AetherStudio 三件套

### 项目：AetherStudio（Rust 编辑器，已逆向）
- **semantic_tokens.rs**：LSP Semantic Tokens 解码器——紧凑 uinteger 数组 → 结构化 token（22 类型 + 10 修饰符），支持 delta 增量更新
- **incremental_sync.rs**：PieceTable 编辑历史 → LSP 增量变更（不全文对比）；FastLineIndex 二分查行 O(log n)；相邻编辑合并；大文件策略（>100KB 延迟同步、变更>50% 发全文）
- **FastLineIndex**：UTF-16 码元计数，处理中文/emoji 正确

### 可抄点
1. **cae_lsp_position_convert 升级**：byte↔position 用 FastLineIndex 算法（O(log n) 二分 + UTF-16 边界处理）
2. **cae_semantic_tokens_decode 升级**：delta 增量解码（现在只解码全量）
3. **code_complete/cae_lsp_query 接进工作流**：改前 code_context → 改后 change_impact → 引用前 lsp_query（三步走规则写进 REASONIX.md）
4. **pyright 作为 Python 语义后端**：lsp_query 的 Python 侧从 pylsp 换 pyright（★15.6k，微软，类型精度高）

### 接入方式
- cae 扩展已有雏形，重点是"接线"（0 调用 → 工作流核心）+ 算法升级

---

## 三、🟠 定位掌握（P1）—— 抄 ast-grep + Qoder Repo Wiki

### 项目1：ast-grep ★15.5k（Rust 结构搜索/改写）
- 用"代码样式的模式"搜索代码（如 `$A + $B` 搜所有加法），支持 lint/rewrite
- 与 unified-rx 关系：**locate_edit 的结构搜索后端**——自然语言→符号→ast-grep 模式→精确位置

### 项目2：Qoder Repo Wiki（官网实证）
- 自动生成代码库 Wiki：目录树→模块→入口→关键符号→依赖（已生成 40 万+）
- **AetherStudio 仓库里就有 `.qoder/repowiki/zh/content/` 产物**（API 参考/UI 系统/架构设计/性能优化——结构化 Wiki 目录树！）

### 可抄点
1. **新工具 `repo_wiki`**：cb_index 索引完 → 生成 markdown 结构文档（对齐 Qoder 的 `.qoder/repowiki/` 结构）
2. **locate_edit 后端加 ast-grep**：符号定位从正则 → 结构感知
3. **Wiki 分级**：架构设计/API 参考/UI 系统/性能优化（AetherStudio 的 Wiki 目录就是模板）

---

## 四、🟠 探索掌握（P1）—— 抄 LATS + ToT + LSE 树引导

### 论文
- **LATS（Language Agent Tree Search）** ICML 2024：MCTS × ReAct × ToT——用值函数引导树搜索，语言模型中统一推理/行动/规划
- **ToT（Tree of Thoughts）** NeurIPS 2023：思维树——每个节点是"思考步骤"，BFS/DFS 探索
- **LSE（Learning to Self-Evolve）** arXiv:2603.18620：树引导进化循环——**你的 lse-engine 已实现**（delta reward + UCB + 跨模型经验）

### 可抄点
1. **bug_locate 的 UCB 升级为 LATS 式**：值函数（value function）引导候选分支选择，而非纯 UCB 奖励
2. **多轮探索**：当前 bug_locate 单轮；LATS 支持"探索-评估-回溯"多轮
3. **LSE 树引导进化增强**：上下文编辑（context edit）奖励 = 下游性能提升——`experience_store` 的 delta 计算可对齐论文

---

## 五、🟠 记忆掌握（P1）—— 抄 mem0/Letta/cognee + LightRAG

### 项目
- **mem0 ★63k**：通用 Agent 记忆层——记忆提取/存储/检索，跨会话
- **Letta ★24k**（原 MemGPT）：有状态 Agent 平台——记忆分层（核心/工作/存档）
- **cognee ★30k**：自托管知识图谱记忆引擎
- **GraphRAG ★35k / LightRAG ★38.8k（EMNLP 2025）**：图结构 RAG

### 可抄点
1. **记忆分层**（Letta 思路）：lesson_recall_lse 加"核心教训/工作记忆/存档"三层——你现在的 LSE 是平铺的
2. **知识图谱记忆**（cognee/GraphRAG 思路）：教训按实体（工具/文件/模式）连边，检索时图遍历而非纯关键词
3. **LightRAG 的轻量**：双级检索（低/高层）——lesson_recall 可按"具体教训/通用原则"分级召回
4. **mem0 的提取管线**：LLM 从对话中自动提取记忆（你现在是手动 lesson_feedback）——**自动回灌**是最大增量

---

## 六、🟢 反馈掌握（P2）—— 抄 AetherStudio 卡片形态

### 已对齐
- `tool_card` 已实现 AiRole::Tool 语义（role/ok/summary/detail）

### 可抄点（UI 形态对齐）
1. **操作卡片**：AetherStudio 的 AiRenderItem（新建/修改/删除/运行命令/读取/列目录，各配图标+标签+主题色）——tool_card 的 detail 增加 `op_kind` 字段
2. **流式卡片**：Generating/Incomplete 状态（生成中/中断）——tool_card 增加 status 枚举
3. **思考过程分离**：AetherStudio 把 reasoning 与 content 分开展示——scan 工具返回的"推理摘要"与"结果"分离

---

## 七、🟠 质量掌握（P1）—— 抄 ruff/semgrep/codeql 规则库

### 项目
- **ruff ★49k**：Rust 写的 Python linter（比 flake8/black 快 100×）
- **semgrep ★16k**：模式即规则的静态分析（规则像源代码）
- **codeql ★9.9k**：GitHub 的语义分析（查询语言 QL）
- **pyright ★15.6k**：微软静态类型检查
- **bandit ★8.2k**：Python 安全扫描
- **gitleaks ★28.6k**：密钥泄露检测

### 可抄点
1. **std_check 规则库换 ruff 引擎**：Python 文件标准检查从正则 → AST 规则（ruff 有现成规则集）
2. **bug_scan 加 semgrep 规则模式**：`pattern: open($F)` + `pattern-not: ...close()` 这种模式匹配比手写 AST 遍历简洁
3. **密钥扫描**：gitleaks 规则库（你现在只有 regex 黑名单）
4. **类型检查**：pyright 进 project_scan 的第四路

### 接入方式
- 全部是独立二进制/库，作为扩展接入（vendor/quality/），不侵入 server.py

---

## 八、🟠 支撑1：搜索（P1）—— 抄 ripgrep/tantivy

| 项目 | ★ | 用途 |
|---|---|---|
| ripgrep | 67k | 最快的 grep（尊重 .gitignore）——替换 fs_list 后的文本搜索 |
| tantivy | 15.7k | Rust 全文索引库（Lucene 风格）——cb_index 的全文层 |
| meilisearch | 59k | 即时全文搜索 API——可选外部服务 |

### 可抄点
1. **全文索引层**：cb_index 加 tantivy（或先 SQLite FTS5）——符号/内容搜索从线性扫 → 索引查询
2. **ripgrep 替换 re 扫描**：bug_scan/std_check 的文件遍历用 rg 加速（子进程，像 rx-core 模式）

---

## 九、🟢 支撑2：性能与部署（P2）

1. **单二进制模式**（codebase-memory 已验证）：rx-core + lse-engine + 核心逻辑 → 一个 Rust 二进制，Python 变薄壳——彻底解决"Python 环境/PATH 问题"
2. **uv ★88.7k**：Python 包管理（比 pip 快 100×）——部署脚本用 uv 替代 pip
3. **内存分层**（AetherStudio 热/温/冷）：scan-log 热数据 mmap → 温 SQLite → 冷压缩——防日志膨胀

---

## 十、🟢 支撑3：评测体系（P2）—— 抄 SWE-bench/AlphaCodium

1. **SWE-bench ★5.6k**（NeurIPS 2024）：真实 GitHub issue 修复基准——unified-rx 的 bug 工具可用它当测试集（挑 Python 子集）
2. **AlphaCodium ★4k**：flow engineering（生成→测试→修正循环）——pipeline 配方的参考
3. **evals ★19k**（OpenAI）：评估框架——工具调用质量回归测试

---

## 十一、路线图（全方位强化，分阶段）

### Phase 1（结构+语义，先搞这两个）
- P0-1: cb_index → 知识图谱升级（tree-sitter + 调用图 + impact analysis）
- P0-2: 新工具 repo_wiki（对齐 Qoder Repo Wiki / codebase-memory 图查询）
- P0-3: cae lsp 三件套接线（0 调用 → 工作流核心）+ FastLineIndex 升级
- 验证：现有 113 pytest + 新增图查询测试 + mcp_smoke

### Phase 2（定位+搜索+质量）
- P1-1: locate_edit 加 ast-grep 结构搜索后端
- P1-2: cb_index 加全文索引（SQLite FTS5 起步）
- P1-3: std_check 换 ruff 引擎 + bug_scan 加 semgrep 模式 + gitleaks 密钥扫描
- 验证：对 VoxelForge-Nexus / reasonix-src 实测扫描对比

### Phase 3（记忆+探索增强）
- P1-4: lesson_recall 分层（核心/工作/存档）+ 自动提取管线
- P1-5: bug_locate UCB → LATS 值函数引导
- 验证：parity + 真实 bug 定位实验

### Phase 4（反馈+性能+评测）
- P2-1: tool_card 卡片形态对齐（op_kind/status/思考分离）
- P2-2: rx-core 单二进制化评估
- P2-3: SWE-bench 子集接入评测
- 验证：全量 pytest + 性能基准

---

## 十二、一句话总结

> **你要的"全方面强"= 结构（知识图谱）+ 语义（LSP）+ 定位（结构搜索）+ 探索（树搜索）+ 记忆（分层进化）+ 反馈（卡片回喂）+ 质量（顶级 lint/scan）七路全开，每路抄业界最强（codebase-memory / AetherStudio / LATS / mem0 / ruff），全部通过 MCP 通道进 unified-rx。**
> 你的东西不是屎山——是七条腿还没站齐的桌子，每条腿的"业界最强版"我都找到了。

---

## 附：调研数据源（全部实查）

- GitHub API（star 数为 2026-08-12 实测）
- arXiv：2603.27277（Codebase-Memory）、2603.18620（Learning to Self-Evolve）——摘要全文实读
- AetherStudio 源码逆向（semantic_tokens/incremental_sync/permissions/ai_panel）
- Qoder 官网 + AetherStudio 仓库内 `.qoder/repowiki/` 产物实证
- codebase-memory-mcp 本地副本 README（D:\开发）
