# unified-rx IDE 全面提升方案

> 2026-08-13 · 文档先行（用户工作流：先方案后代码）
> 目标：IDE 语义掌握能力"全部提升"——对标 AetherStudio/Qoder/codebase-memory-mcp 的顶级实现
>
> **状态：R0-R7 全部落地 ✅（2026-08-13）**——172 测试全过，工具 42→47，E: 运行版已同步

---

## 〇、现状盘点（已有什么）

### cae 扩展 13 工具（vendor/extensions/code-analysis-enhance）
| 工具 | 能力 |
|---|---|
| lsp_query | LSP 查询（诊断/符号/引用/悬停/定义） |
| lsp_semantic_tokens_decode | 语义 token 解码（着色数据） |
| lsp_position_convert | 行列 ↔ 偏移转换 |
| lsp_edit_merge | 编辑合并（多 agent 协同） |
| aether_goto_parse | 跳转定义解析 |
| aether_agent_parse | agent 输出解析（AETHER 标记） |
| aether_probe | 环境探测 |
| aether_lang_support | 语言支持查询 |
| aether_model_provider | 模型提供者配置 |
| change_impact | 变更影响分析 |
| code_context | 光标上下文（+跨文件引用链） |
| lesson_recall | 教训召回 |
| file_dedup_state | 文件去重状态 |

### 核心 42 工具中的 IDE 相关
code_complete / repo_graph（符号图）/ repo_wiki（结构文档）/ bug_scan / locate_edit / cb_scan / quality_scan / kb_query

### 语义 preset 3 个（pipeline）
semantic_before / semantic_after / semantic_edit（5 步闭环：上下文→诊断→修复→验证→教训）

---

## 一、对标分析（顶级实现抄什么）

| 参考 | 核心机制 | 抄什么 |
|---|---|---|
| **AetherStudio**（91★，已逆向） | aether-lsp：semantic_tokens 22 类型 + **delta 增量**；**PieceTable + FastLineIndex** 文档模型；ai_hot_data **热温冷三层**；L1-L4 权限 | 增量同步 / 热温冷 / 权限分级 |
| **Qoder** | Repo Wiki（40 万库）+ Quest 模式 + 100k 文件上下文 | 结构文档（已有 repo_wiki） + Quest 任务化 |
| **codebase-memory-mcp**（38.6k★） | tree-sitter 知识图谱 + LSP + 15 工具 | 图谱融合（已有 graph_index） |
| **ruff/semgrep**（已接入 quality_engine） | 诊断后端 | 已有 |
| **tree-sitter 18 语言**（graph_index） | 符号图 | 已有 |

**结论**：现有基础扎实（图/检索/质量/LSE 都落地），IDE 缺口在：**LSP 增量同步、热温冷缓存、权限分级、语义全家桶补全（rename/hover/completion）、编辑会话模型（PieceTable）**。

---

## 二、提升蓝图（7 大提升点）

### P1：LSP 增量同步（省 token 关键）
```
现状：每次全量诊断/符号（浪费 token + 慢）
提升：文件版本跟踪（mtime+hash）→ 仅改动文件重新诊断 → 未变文件用缓存
收益：大仓库 IDE 操作 token 省 90%+，响应 <50ms
```

### P2：热温冷三层缓存（抄 AetherStudio ai_hot_data）
```
热（活跃文件集）：LSP 诊断/符号/token 全缓存，内存态，每次即时
温（项目内未活跃）：SQLite 落盘（已有 storage_tiers 温层），访问时升热
冷（历史仓库/归档）：压缩态 + 摘要（已有冷层 gzip），仅索引级可见
衔接：storage_tiers.py 已有三层基建 → 接 IDE 数据流
```

### P3：权限分级（抄 AetherStudio L1-L4）
```
L1 只读查询（诊断/符号/悬停——默认）
L2 只读深查（引用链/影响面——可调 repo_graph/change_impact）
L3 建议修改（locate_edit/bug 修复方案——不落盘）
L4 写操作（lsp_edit_merge/fs_write——需显式授权）
衔接：server.py 已有 _check_path 沙盒 → 扩展为权限层
```

### P4：语义全家桶补全（对齐 LSP 标准能力）
```
已有：诊断/符号/引用/定义（lsp_query）
补：
  hover（悬停文档——符号类型+注释+定义摘要）
  rename（安全重命名——符号图 callers/callees 联动验证）
  completion（补全——tree-sitter 同库符号 + 类型上下文）
  signature_help（参数提示）
  code_action（快速修复——quality_engine 规则 → 建议列表）
```

### P5：编辑会话模型（抄 AetherStudio PieceTable）
```
文本模型：PieceTable 增量编辑（不重写全文）+ FastLineIndex 快速行列定位
衔接：lsp_position_convert 已有 → 升级为 O(log n) 行索引
应用：大文件（interaction.rs 5009 行）编辑场景，只算增量 diff
```

### P6：IDE 结果融合进掌握引擎（已有能力的深度整合）
```
LSP 诊断 → graph_index 符号节点（问题标注在图上）
语义 token → search_index 索引（着色级检索）
hover/补全数据 → lesson_extract 训练语料
change_impact → repo_graph impact 的双引擎校验（LSP 引用 vs tree-sitter 调用）
```

### P7：Quest 任务化（抄 Qoder）
```
"修这个 bug" → Quest 链：诊断 → 定位 → 影响面 → 修复建议 → 验证 → 教训
衔接：已有 semantic_edit preset（5 步）→ 升级为可断点续跑的 Quest 状态机
```

---

## 三、分阶段路线

| 阶段 | 内容 | 依赖 | 验证 |
|---|---|---|---|
| **R0（先做）** | IDE 现状测试基线（13 工具 smoke + preset 验证） | 无 | pytest |
| **R1** | LSP 增量同步（版本跟踪 + 缓存） | 无 | 同文件二次查询耗时对比 |
| **R2** | 权限分级 L1-L4（_check_path 扩展） | 无 | 越权拒绝测试 |
| **R3** | 热温冷接 IDE 数据流（storage_tiers 复用） | R1 | 三层命中率 |
| **R4** | 语义全家桶：hover/rename/completion/code_action | R1 | 新工具单测 |
| **R5** | 编辑会话模型（PieceTable + FastLineIndex） | R4 | 大文件增量 diff |
| **R6** | IDE→图/索引/教训融合 | R1-R4 | 融合一致性 |
| **R7** | Quest 状态机 | 全 | 端到端任务 |

**原则**：每阶段独立可回滚 + 棘轮测试同步（工具数变化）+ 运行版 E: 同步。

---

## 四、验收标准

- [ ] 13 现有工具回归全过 + 新增工具测试
- [ ] 大仓库（VoxelForge）IDE 操作：二次查询 token 省 90%+
- [ ] 权限：L4 写操作显式授权，越权拒绝
- [ ] 热温冷：热命中 >95%，冷访问降级不炸
- [ ] 全家桶 5 工具（hover/rename/completion/signature/code_action）实测可用
- [ ] PieceTable 编辑 5000 行文件 diff 正确
- [ ] IDE 诊断在 repo_graph 图上可见（融合）

---

## 五、风险与诚实缺点

| 风险 | 应对 |
|---|---|
| LSP 服务器不可用（环境无 clangd/rust-analyzer） | 降级 tree-sitter 只读模式（已有 18 语言） |
| 增量同步误判（文件外部改动） | mtime+hash 双检 |
| 缓存膨胀 | 热层 LRU 上限 + 温层定期 vacuum |
| 权限分级过度复杂 | L1/L4 两级起步，中间层按需加 |
| PieceTable 实现成本高 | 先做 FastLineIndex（行列 O(log n)），PieceTable 放 R5 末 |
| rename 安全性 | 符号图 callers/callees 全覆盖验证后才落盘 |

---

## 一句话

> **IDE 提升 = 增量同步（省 token）+ 热温冷（快）+ 权限（安全）+ 全家桶（全）+ 会话模型（大文件）+
> 融合（图/索引/教训）+ Quest（任务化）——每项都接现有基建（storage_tiers/_check_path/
> graph_index/semantic preset），不做重复轮子。**
