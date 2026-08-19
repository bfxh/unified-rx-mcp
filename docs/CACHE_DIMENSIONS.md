# 7 维缓存方案评估与落地（CACHE_DIMENSIONS.md）

> 2026-08-19 · 用户提供 7 维缓存方案（语义模糊缓存/推测草稿/AST 结构/几何结果/
> 负向错误/动态频率/潜空间缓存），要求"考虑各种情况再加，还要考虑高质量"。
> 对照本仓库缓存原则（正确性优先/真省 token/有风险不交付）逐维裁决，
> 并给出**安全落地点**（用户："有些有风险的可以看看其他的地方 反正总有落脚点"）。

---

## 裁决总表

| 维度 | 方案 | 判定 | 理由 | 落地点 |
|---|---|---|---|---|
| 1 | 语义级模糊缓存（embedding 向量相似 >0.92 复用推理路径） | **不交付** | 复用"相似但不同"的推理路径有正确性风险——语义相似 ≠ 答案相同；违反缓存原则（宁可 miss 不可错） | 无 |
| 2 | 推测草稿缓存（50M 微型 Transformer 草稿器） | **已有等价** | 本仓库 `speculate.py` 已实现推测执行（白名单只读工具命中跳过重执行）——非 LLM 草稿器，但方向一致且零依赖 | `speculate.py`（维持） |
| 3 | AST 语法树结构缓存 | **已有等价** | `cb_index`（tree-sitter 符号图）+ `scan_cache`（mtime+size 键）已缓存结构化分析结果 | `cb_index` / `scan_cache` |
| 4 | 几何计算结果缓存 | **✅ 本次落地** | 纯确定性（同文件同解析结果），零正确性风险；mesh_* 每个工具都先 load_mesh 全量解析（≤64MB）——重复解析是真实浪费 | `geometry_tools.load_mesh` 解析缓存（mtime+size 键、深拷贝保类型、成功才缓存、64 条 LRU） |
| 5 | 负向错误缓存（Bug 模式打压） | **已有等价** | `vuln_rules.json`（模板规则 DSL）+ bug_scan 五类规则 + LSE 教训回灌（refuted 幻觉负 delta） | `vuln_rules.json` / `lse_client` |
| 6 | 动态频率自适应缓存（LFU 热更新） | **部分采用** | 现有 scan_cache 是 LRU（512 条）+ TTL；任务级热更新复杂化收益低——保持 LRU（简单可靠） | `scan_cache`（维持） |
| 7 | 连续潜空间缓存（KVCache 跳过前 N 层） | **不交付** | 依赖 LLM 内部表示（模型特定、版本耦合）；本仓库是 MCP 工具集不托管 LLM 推理——无承载点 | 无 |

## 落地实现（维度 4：几何结果缓存）

### 设计约束（对照缓存原则）

1. **确定性**：同文件 + 同版本（mtime_ns + size + 扩展名）→ 同解析结果，绝不近似。
2. **成功才缓存**：解析失败/畸形文件不缓存（失败原因可能因环境变化）。
3. **深拷贝保类型**：`copy.deepcopy` 返回——vertices/faces 的 tuple 类型契约不被
   JSON round-trip 破坏（初版用 json.dumps/loads 曾把 tuple 变 list，已修）。
4. **上限与失效**：64 条 LRU（进程内）；文件任何变化即失效（宁可 miss 不可错）。
5. **零依赖**：纯 std lib（os/stat/copy），无新增依赖。

### 代码位置

`geometry_tools.py`：
- `_MESH_CACHE`（dict: path → (mtime,size,ext), result）+ `_MESH_CACHE_MAX = 64`
- `_load_mesh_cached(path)`：命中 deepcopy 返回；未命中 None
- `_store_mesh_cached(path, result)`：ok 才存；超上限删最早插入
- `load_mesh(path)` 入口先查缓存，未命中走原解析路径后存入

### 验证

- `test_geometry_tools.py::test_load_mesh_cache_semantics`：命中/深拷贝隔离/
  文件变更失效/失败不缓存四断言
- 语义回归新增锚点：`load_mesh 几何解析缓存命中（维度4 落地）`
- 实测：命中耗时 0ms（原解析随文件大小线性）

## 为什么不交付维度 1/7（向用户说明）

- **维度 1（语义模糊缓存）**：余弦相似 >0.92 复用推理路径——0.92 阈值下
  "相似表述"仍可能语义不同（如"反转数组"vs"反转字符串"），错误缓存结果
  是 bug 而 miss 只是慢。用户既定原则："有任一风险 → 不做/砍掉"。
- **维度 7（潜空间缓存）**：需要访问 LLM 中间层隐藏状态——本仓库是 MCP
  工具集（不托管 LLM 推理），且 KVCache 结构随模型版本变化，缓存跨版本
  即失效——无承载点且高风险。
- 两者的"落脚点"：维度 1 的正确性需求由 `hallucination_guard`（本地证据
  验证）满足；维度 7 的提速需求由工具结果缓存（scan_cache/mesh 缓存）
  在确定性边界内满足——**宁可用确定性缓存换 30% 命中，不用有风险缓存
  换 90% 命中但可能出错**。

## 后续可安全扩展（按需求再开）

- mesh_check/voxelize 等**派生结果**缓存（在 load_mesh 缓存之上加
  (op, 参数) 键——仍确定性；需评估实际调用频率再决定是否值得）
- 跨进程持久化（当前进程内 64 条——跨会话复用需持久化，评估磁盘/加载成本）

---

## 追加：维度 8-14 裁决（2026-08-19 第二轮，用户："这些都看看"）

> 用户补充 7 个"非主流硬核"维度（算子融合/阵列展开/早退分类/记忆压缩/
> 大页内存/逆缓存/时间旅行）。沿用同一框架裁决：正确性优先、真省 token、
> 有风险不交付、无承载点不硬做。

### 裁决总表（8-14）

| 维度 | 方案 | 判定 | 理由 | 落地点 |
|---|---|---|---|---|
| 8 | 算子融合缓存（复合变换矩阵执行计划） | **✅ 本次落地** | 纯确定性（矩阵合成同参数同结果）；"平移→旋转→缩放"连续变换在 3D 代码中高频出现——合成矩阵=缓存执行计划 | `geometry_tools.transform_compose`（4x4 行主序、glTF TRS 惯例、NaN 校验、128 条缓存键=参数序列） |
| 9 | 分形/阵列展开缓存（重复结构坐标生成器） | **✅ 本次落地** | 确定性（同模式同参数同坐标）；LLM 生成重复 3D 结构（10x10 阵列/环形/空间填充）可直接引用模式免循环 Token | `geometry_tools.pattern_expand`（grid/ring/hilbert；hilbert 4^4=256 段封顶防 DoS；128 条缓存） |
| 10 | 早退分类器（跳过 LLM 中间层） | **不交付** | 依赖 LLM 内部隐藏状态+分类头——本仓库是 MCP 工具集不托管推理；分类置信度阈值有正确性风险 | 无 |
| 11 | 记忆压缩（MemGPT 式摘要 Token） | **已有等价** | LSE 教训引擎（`lse_client` delta_update/experience_store 摘要级记忆）+ scan-log 摘要（日志闯进调用）已实现"摘要级记忆窗口无限" | `lse_client` / `scan_log_core`（维持） |
| 12 | 大页内存绑定（TLB 优化） | **不交付** | 硬件级优化针对 LLM KVCache 连续内存——本仓库 Python 工具集无此内存形态；Windows 大页需管理员权限且平台耦合 | 无 |
| 13 | 逆缓存（崩溃边界提前规避） | **已有等价** | mesh_check（非流形/破面/孤立顶点）+ bug_scan 五类规则 + vuln_rules.json + LSE refuted 负 delta 回灌——"坑位规避"全链路已实现 | `mesh_check` / `bug_scan` / `vuln_rules.json` / `lse_client`（维持） |
| 14 | 时间旅行缓存（Undo/Redo 快照） | **已有等价** | `backup`（git 快照+rollback 前自动另存）+ `replay_record`/`replay_run`（状态录制回放）——交互场景状态恢复已实现 | `backup` / `replay_*`（维持） |

### 本次落地细节（维度 8/9）

- `transform_compose(transforms, round_digits=6)`：
  - 支持 translate/rotate(x|y|z, angle_deg)/scale(均匀简写)/matrix(16 元素)
  - glTF TRS 惯例：v' = M₁·M₂·...·Mₙ·v（列表末尾先作用）
  - 缓存键 = JSON 序列化参数；NaN/零缩放/未知类型/非 16 元素全部结构化拒绝
- `pattern_expand(pattern, rows, cols, spacing, center)`：
  - grid（矩形阵列）/ ring（环形）/ hilbert（希尔伯特曲线，depth≤4 防爆炸）
  - rows/cols ∈ [1,200]、spacing 非零有限、center 有限——全部校验
  - 缓存键 = (pattern, rows, cols, spacing, center)
- 验证：`test_transform_compose_semantics`（TRS 语义点乘断言 + 缓存命中 +
  4 错误契约）、`test_pattern_expand_semantics`（数量/坐标/缓存/封顶/3 错误契约）；
  语义回归 124 锚点新增 2 条

### 14 维总览（1-14 全部裁决后）

**落地 3**：维度 4（几何解析缓存）、维度 8（变换合成）、维度 9（阵列展开）
**已有等价 6**：维度 2（speculate）、3（cb_index/scan_cache）、5（vuln_rules/LSE）、
11（LSE/scan-log）、13（mesh_check/bug_scan）、14（backup/replay）
**部分采用 1**：维度 6（scan_cache LRU 维持）
**不交付 4**：维度 1（语义模糊）、7（潜空间）、10（早退）、12（大页内存）
——共同点：依赖 LLM 内部表示或近似复用，无承载点或正确性风险；
"落脚点"一律用确定性缓存（宁 miss 不可错）替代。
