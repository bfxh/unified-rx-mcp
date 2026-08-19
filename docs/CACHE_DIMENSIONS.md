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

---

## 追加二：维度 15-100 + 可微渲染（2026-08-19 第三轮，用户："等等先搞这些"）

> 用户连续贴出：维度 15-32（#3-#4 硬核底层）、12 通用小手术刀（#5）、20 个
> 3D 小手术刀（#6）、65-84 技术点（#7）、85-100 元缓存（#8）、可微分渲染
> （#9）。用户确认"都搞（缓存优先，可微渲染跟进）"。

### 维度 15-32 裁决

| 维度 | 判定 | 理由/落地点 |
|---|---|---|
| 15 KL 散度缓存（跳 softmax） | 不交付 | 依赖 LLM 输出头内部——无承载点 |
| 16 JIT 编译缓存（LLVM IR） | 不交付 | 无 LLM 代码执行链承载 |
| 17 傅里叶域缓存 | 不交付 | 频域系数近似还原有正确性风险 |
| 18 熵状态缓存 | 不交付 | LLM 内部信息熵监控——无承载点 |
| 19 符号回归缓存 | 不交付 | 需 Eureqa 类依赖 + 归纳近似风险 |
| 20 原型继承缓存（跨会话原型库） | 部分落地 | pattern_expand（grid/ring/hilbert）已建模式库；SQLite 全局原型=未来方向 |
| 21 零拷贝共享内存 | 不交付 | OS/硬件级，无承载点 |
| 22 时序因果图缓存 | 不交付 | LLM 内部因果子图——无承载点 |
| 23 空间八叉树节点缓存 | 部分落地 | voxelize 结果缓存已建（占用模式哈希等价） |
| 24 潜空间流形插值 | 不交付 | 依赖 LLM 隐空间 |
| 25 动量缓存 | 不交付 | 训练级，本仓库无训练 |
| 26 注意力头分支预测 | 不交付 | 硬件级 |
| 27 跨进程零拷贝 | 不交付 | OS/网络级 |
| 28 算术编码码本 | 不交付 | LLM 输出层信息论压缩 |
| 29 元认知反思缓存 | 不交付 | LLM 内部验证头 |
| 30 递归自修改 | 不交付 | 元学习器+自修改策略=正确性风险+过度工程 |
| 31 全息投影缓存 | 不交付 | 物理级近似还原风险 |
| 32 虚空缓存 | 已有等价 | "继续/一样"返回上轮输出=scan_cache 工具结果缓存 |

### 12 通用小手术刀裁决（#5）

| # | 技术 | 判定 | 理由 |
|---|---|---|---|
| 1 | JSON 字段名压缩 | 不交付 | 工具输出 JSON 是**契约**（字段名被 AI/解析器依赖）——压缩破坏契约 |
| 2 | 停止符精确制导 | 不适用 | 本仓库不调用 LLM API（stdio 工具集） |
| 3 | 输入宏替换 | 不适用 | 同上 |
| 4 | lru_cache 挂载本地工具 | 部分落地 | geometry_tools 各缓存（mesh/voxel/ray/bbox/mass）即此思想；纯函数 μs 级无额外收益 |
| 5 | MD5 去重缓存 | 已有等价 | scan_cache（幂等只读工具，mtime+size 键） |
| 6 | temperature=0 | 不适用 | 无 LLM API |
| 7 | 输出 Schema | 不适用 | 同上 |
| 8 | 锚点固定 | 不适用 | 同上 |
| 9 | 数值精度降级 | 不交付 | 几何精度是正确性契约（坐标保留 6 位） |
| 10 | 中间状态复用 | 不适用 | 无本地推理 |
| 11 | 字符串常量池 | 低价值 | 只压缩本地日志体积，不省 token |
| 12 | Gzip | 不适用 | stdio 协议非 HTTP |

### 20 个 3D 小手术刀裁决（#6）

| # | 技术 | 判定 | 落地点 |
|---|---|---|---|
| 1 | 顶点量化输出 | 不交付 | 精度契约（同 #9） |
| 2 | 索引几何体 | ✅ 已实现 | faces=(i,j,k) 索引结构 |
| 3 | 实例化变换 | ✅ 已实现 | pattern_expand（维度 9） |
| 4 | 场景图相对坐标 | ✅ 已实现 | 局部坐标语义 |
| 5 | 包围盒缓存 | **✅ 本次落地** | `mesh_bbox`（文件签名键） |
| 6 | 法线/UV 模板 | 已有等价 | mesh_splat normal_tensor |
| 7 | LOD 多级缓存 | 已有等价 | mesh_optimize |
| 8 | 对称性生成 | 未来方向 | 镜像工具可加 |
| 9 | 阵列模式 | ✅ 已实现 | pattern_expand |
| 10 | 常量预编译 | 低价值 | PI/RAD 常量表 |
| 11 | 矩阵融合 | ✅ 已实现 | transform_compose（维度 8） |
| 12 | 柯里化简化 | 低价值 | 变换链合并=transform_compose |
| 13 | 宏定义 | 不适用 | LLM 输出层 |
| 14 | 短别名 | 不适用 | 同上 |
| 15 | 内置函数调用 | ✅ 已实现 | mesh_* 工具族即库调用 |
| 16 | 射线-三角形相交缓存 | **✅ 本次落地** | `_RAY_HIT_CACHE`（顶点坐标量化键，4096 条） |
| 17 | KNN 缓存 | 未来方向 | 点云场景再开 |
| 18 | 体素化结果缓存 | **✅ 本次落地** | `_VOXEL_CACHE`（文件签名+resolution 键，64 条） |
| 19 | SDF 采样缓存 | 未来方向 | 隐式曲面场景再开 |
| 20 | 绘制命令打包 | 不适用 | 渲染层 |

### 65-84 技术点裁决（#7）：8 项已有等价（AST 折叠=transform_compose 单位剔除、
67 B-rep=half_edge、75 日志采样=_scan_log_tick 5s 采样、76 **✅ 本次落地
mesh_mass_props**、77 关键帧=动画层、81 简化误差=mesh_optimize、82 UV 模板=
mesh_splat、84 碰撞代理=bbox）、其余（SIMD/网络/内存/硬件/资产层）不适用或未来。

### 85-100 元缓存裁决（#8）：86 二进制差分=backup 快照、87 种子复用可加
（低价值）、90 禁忌缓存=bug_scan/vuln_rules/LSE、91 管道拼接=pipeline 工具、
93 空操作=scan_cache、97 CRC 去重=scan_cache 文件签名——其余（惰性树/潜空间/
玻尔兹曼/递归自指等）依赖 LLM 内部或自修改策略，不交付。

### #9 可微渲染落地（数据基础设施）

| 组件 | 落地 | 说明 |
|---|---|---|
| ① 可微表示 | mesh_splat（已有） | 顶点/面/法线参数张量 |
| ② 渲染器 | **✅ render_depth** | 软光栅正交投影（front/top/side），z-buffer 深度图 |
| ③ 损失 | **✅ render_loss** | L1（render vs target 矩阵） |
| ④ 梯度 | **✅ render_gradient** | 有限差分 ∂loss/∂v（.obj 扰动重渲染，临时文件自动清理） |
| ⑤ 优化 | 未来方向 | 沿负梯度更新顶点循环；真·GPU 反向传播（Nvdiffrast 级）需 GPU 框架 |

验证：pytest 24/24（新增 test_mesh_bbox_and_mass_props / test_render_depth_loss_gradient /
test_voxelize_and_ray_cache）；语义回归新增锚点待跑。

---

## 追加三：碰撞检测 + 拓扑持久同调 + #11/#12 评估（2026-08-19 第四轮）

> 用户："上面剩下的还有什么没有搞的就搞 然后还有碰撞检测库：如FCL、Parry等，
> 用于精确的碰撞检测"。随后贴出 #10（3D 检测工具链）、#11（10 个数学/算法框架）、
> #12（101-140 生物/物理/数学/认知启发）。

### 本次落地 2 项（全确定性）

| 项 | 来源 | 说明 |
|---|---|---|
| `collision_check` | 用户点名（FCL/Parry 概念零依赖） | 四模式：mesh-mesh（AABB 粗筛 + Möller 三角形对精确相交，采样上限防爆炸）/ mesh-point（射线奇偶）/ mesh-aabb；三角形对结果缓存（量化键 4096 条） |
| `mesh_betti` | #11 技术1 简化版 | Betti 数：β0 连通分量（并查集）/ β1 环 / β2 空腔（欧拉公式），闭合性判定——"拓扑体检"标量 |

**顺带修复**：`_point_in_mesh` 射线奇偶测试共享边重复计数 bug（中心点
(0.5,0.5,0.5) 误判外部——射线穿过 x=1 面两个三角形同 t 计 2 次）；改为
收集 t 值**去重后**判奇偶。`_ray_tri_intersect` 返回 t（float|None）而非 bool。

### #10 检测工具链裁决（用户提供清单）

| 工具 | 判定 | 落地点 |
|---|---|---|
| FCL/Parry（精确碰撞） | ✅ 已落地 | collision_check（零依赖自研等价） |
| CalculiX（有限元） | 不交付 | 重型外部依赖；质量属性标量用 mesh_mass_props |
| Godot/Rapier Testbed | 不交付 | 外部引擎测试台 |
| PxCollision | 不交付 | 外部工具 |
| OpenGeode-Inspector | 已有等价 | mesh_check（非流形/边界边/孤立顶点）+ mesh_betti |
| Scanalyzer（Open3D） | 已有等价 | mesh_mass_props + mesh_bbox |
| Blender/MeshLab 检查 | 已有等价 | mesh_check |
| glTF Validator | 已有等价 | _parse_glb（格式解析）+ 结构校验 |
| gltf-inspector | 不适用 | 浏览器工具 |
| MeshGrab | 不适用 | Chrome 扩展 |
| Asset Auditor / 3DDFM | 不适用 | 云端/制造级 |

### #11 十算法裁决

| 技术 | 判定 | 理由 |
|---|---|---|
| 1 拓扑持久同调 | ✅ 简化落地 | mesh_betti（Betti 数；完整 Persistence Diagram 需滤复形，标注未来） |
| 2 等几何分析 IGA | 不交付 | NURBS 刚度矩阵=重型数学内核 |
| 3 伴随灵敏度 | 部分落地 | render_gradient 有限差分；伴随法反向求解需 GPU 框架（未来） |
| 4 ROM 降阶 | 不交付 | 需 SVD 数据集合成（无训练数据源） |
| 5 神经算子 FNO | 不交付 | 需训练网络+数据（有依赖） |
| 6 谱元冻结 | 不交付 | FEA 域 |
| 7 SPH 流体 | 不交付 | 流体域（超出几何工具范围） |
| 8 核岭回归外推 | 不交付 | 数据驱动代理需数据集 |
| 9 混沌映射 PCE | 不交付 | 蒙特卡洛统计域 |
| 10 因果反事实 | 不交付 | 因果图需领域知识建模 |

### #12 生物/物理/数学启发 101-140 裁决

全部 40 项（蚁群信息素/免疫克隆/突触可塑性/遗传进化/相变/布朗运动/量子隧穿/
同胚/测地线/贝叶斯先验/生态演化/图式/双进程/忆阻器/全息对偶/量子纠缠等）——
**统一不交付**，理由：①几乎全部是"软匹配/概率吸引"型缓存——命中可能返回
"相似但不同"的结果，违反正确性优先原则（维度 1 同款裁决）；②多数需外部
框架（SNN/忆阻器/GPU/神经算子）或有正确性风险（同胚/全息/量子）；③现有
确定性缓存（scan_cache/mesh 系列）已覆盖其安全子集（如 118 生态位=多级缓存
分层、121 图式=pattern_expand 模式库、134 红移=TTL 时效）。
**唯一可提取原则**（入档不实现）："任何能减少不确定性、增加可预测性的
机制都可转化为缓存策略"——但落地必须保持**精确匹配/确定性**，不用近似。

---

## 追加四：#13-#16 评估（2026-08-19 第五轮）

> 用户贴出 #13（101-140，与 #12 **完全相同**——已裁决，不重复评估）、
> #14/#15（3D 检测工具清单 + 可微分动画前沿）、#16（可微分物理 20 方向）。

### #13 与 #12 重复确认
#13 全文与上轮 #12（101-140 生物/物理/数学启发）一字不差——40 项统一不交付
裁决仍有效（软匹配/概率吸引违反正确性优先）。唯一可提取原则已入档：
"确定性落地，不用近似"。

### #14/#15 检测工具清单裁决（新增量）

| 工具/指标 | 判定 | 落地点 |
|---|---|---|
| neatmesh（体积/表面积/法线） | ✅ 本次落地 | mesh_mass_props 增 `surface_area` + `surface_volume_ratio`（单位立方体 6.0 验证） |
| Trimesh watertight（水密） | 已有等价 | mesh_betti `closed`（boundary_edges==0） |
| val3dity ISO19107 合规 | 已有等价 | mesh_check（拓扑）+ mesh_betti（闭合性） |
| Blender 3D Print Toolbox / MeshLab / Netfabb / Meshmixer | 已有等价 | mesh_check（非流形/洞/孤立顶点）+ mesh_betti + collision_check |
| glTF Validator / gltf-inspector | 已有等价 | _parse_glb（结构校验） |
| CloudCompare / GOM / Geomagic 偏差分析 | 不适用 | 扫描-模型比对需参考数据（未来方向） |
| CAiD MCP Server / OpenGeode | 已有等价 | geometry_tools 工具族（布尔/倒角/抽壳对应 mesh_boolean/mesh_clip 等） |
| 壁厚/悬垂检测（3D 打印） | 未来方向 | 需 SDF 采样或射线厚度分析（#6-19 同类） |

### #15/#16 可微分动画/物理前沿裁决

PhysRig / SNARF / Puppeteer / DiffMimic / Genesis / Newton / phyz / Dojo /
DiffAvatar / PhysGaussian / DiffMPM 等 20+ 方向——**统一不交付**，理由：
①全部需 GPU 框架（Warp/JAX/CUDA）或重型引擎（违反零依赖）；②神经训练需
数据集（无源）；③本仓库的可微能力已落地为数据基础设施（render_depth/
render_loss/render_gradient 有限差分）。趋势入档：LBS 蒙皮被"神经-物理"
可微分框架取代——与本仓库 mesh_splat（可训练参数表）方向一致，标注未来。

---

## 追加五：LBS 蒙皮 + 可微分梯度（趋势落地，2026-08-19 第六轮）

> 用户："那开搞啊"（选定上下文："趋势入档：LBS 蒙皮 → 神经-物理可微分框架"）
> ——把 #15/#16 可微分动画/物理前沿（PhysRig/SNARF/Puppeteer/DiffMimic）中
> 的确定性数学部分落地为零依赖数据基础设施。

### 落地 2 项（全确定性公式）

| 项 | 说明 |
|---|---|
| `skin_deform` | 标准 LBS 公式 v' = Σ wᵢ·(Mᵢ·Mᵢ⁻¹ᵇ·v)：骨骼矩阵（transform_compose 可生成）+ 权重（逐顶点或 #83 模板 default_human/tentacle/rigid）；文件签名+参数序列缓存；NaN/骨骼越界/权重非有限校验。实测：单位矩阵不变形、平移骨跟随、0.5/0.5 混合插值精确 5.0 |
| `skin_gradient` | 可微分蒙皮：∂v'/∂wᵢ 有限差分（原始权重语义 = 该骨单独作用位置 Mᵢ·Mᵢ⁻¹ᵇ·v——梯度方向即"该骨骼拉动顶点的方向"，直观）。权重=可训练参数，与 mesh_splat 参数张量方向一致 |

### 趋势映射

- LBS 蒙皮（本落地）→ SNARF（神经隐式蒙皮，解析梯度）→ PhysRig（MPM 物理
  绑定）→ 全可微分物理（Genesis/Newton）——梯度链逐级增强，本落地是最底层
  的确定性台阶（零依赖、可验证、无训练数据需求）。
- 与已落地链路闭环：transform_compose（骨骼矩阵）→ skin_deform（变形）→
  skin_gradient（可训练参数梯度）→ render_depth/loss/gradient（渲染监督）——
  一个"参数化 3D 资产优化"的最小可微分管线已成型。

### 验证

pytest 190/190（新增 test_lbs_skin_deform_and_gradient：不变形/跟随/插值/
模板/双梯度数值/三错误契约）；语义回归 127/127（新增锚点）；扫描 12 条=
基线 10+2 finally 容错。
