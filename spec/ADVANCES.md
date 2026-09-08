# ADVANCES —— 外部技术雷达：能大幅提升本工具箱的东西（S100 调研轮）

> 目的：回答"有没有什么东西能大幅提升这些工具"。方法：查开源实现 + 论文，
> 逐项对照本仓现状（**只列还没有的**），给出预期增益、落地成本、红线影响与优先级。
> 调研日期 2026-09-09；本文件是**建议清单**，不是承诺——每项落地仍走仓库纪律
> （oracle 对照 → 双绿 → 文档）。
>
> 红线前提（不因本文档改变）：Python 纯 stdlib；Rust `[dependencies]` 恒空；
> 每轮 pytest + cargo test 双绿；不换模型；沙盒语义两侧等价。

## 一、结论速览

| # | 技术 | 目标工具 | 预期增益 | 成本 | 红线 | 优先级 |
|---|---|---|---|---|---|---|
| 1 | **repo_map**（个人化 PageRank 符号地图） | 新增工具（agent 上下文选择） | RepoGraph 论文口径平均相对 +32.8%；减少"瞎翻文件" | 1 轮 | 零依赖可行 | ✅ **S102 已兑** |
| 2 | **混合检索 RRF**（BM25 ⊕ 语义路） | code_search | 融合优于单路（RRF k=60 为默认） | 0.5 轮 | 纯算法 | ✅ **S101 已兑** |
| 3 | **查询侧根词约束**（子词索引假阳性防护） | code_search | 召回不变、假阳性下降 | 0.2 轮 | 纯算法 | ✅ **S101 已兑** |
| 4 | **内容寻址增量缓存**（salsa 思想） | scan/search 全域 | 重复调用延迟降一个数量级（增量分析文献 1.3–68×） | 1 轮 | 零依赖可行 | ✅ **S103 已兑** |
| 5 | **测试影响分析**（Ekstazi 文件指纹 RTS） | ide_test | 测试时间 −32%~54%（Ekstazi 实测） | 0.5–1 轮 | 零依赖可行 | ✅ **S104 已兑** |
| 6 | **切片式上下文包**（ARISE/SliceMate 思路） | code_context 系 | 上下文 token −23%~54%（SWE-Pruner 实测） | 1–2 轮 | 零依赖可行（近似切片） | **P1** |
| 7 | **栈图式名字解析**（stack graphs） | dep_graph / ide_impact / 调用图 | 不依赖 LSP 的语义级定义/引用（GitHub 生产级方案） | 2 轮 | 零依赖可行（简化版） | **P2** |
| 8 | **ACI 输出纪律复核** | 全部工具出口 | 接口设计本身值 +10.7pp（SWE-agent 消融） | 0.5 轮 | 纯约定 | **P1** |
| 9 | **漏洞知识库（Vul-RAG 式）** | bug_scan / code_review 解释面 | 检测准确率 +12.96%、人工复核 60%→77% | 建库成本高 | 零依赖可行 | **P2** |
| 10 | **tree-sitter/ast-grep 可选引擎** | scan 家族 | 语言覆盖 20+、规则即模式 | 1 轮 | 可选依赖+降级 | **P2** |
| 11 | **SCIP 索引消费** | ide_impact / 引用面 | 语义级引用，不起 LSP 会话 | 1–2 轮 | 手写 protobuf 可行 | **P2** |

**已在本仓落地、无需重做**（避免重复劳动）：子词切分（camelCase/snake_case/中文
bigram，`rust/src/search.rs:194`）、符号级 rerank 与指纹缓存（S12/S13）、
输出条数/字符串钳制（registry `_clamp`）、编辑语法门（ide_edit S55）、
可达性分级（rust_reach S16）、双臂 A/B 与 SWE-bench 外锚（bench/）。

## 二、逐项详解

### 1. repo_map：给 agent 一张"按相关度裁剪的仓库地图"（✅ S102 已兑）
- **原理**：抽取符号定义与引用建图 → 以"当前正在改的文件/对话涉及的文件"为
  种子做**个人化 PageRank**（Random Walk with Restart）→ 按 token 预算二分裁剪，
  输出最相关的定义骨架。代表实现：aider `repomap.py`、Rust 版
  [repo-mapper](https://github.com/mackenney/repo-mapper)、
  [repomap-mcp](https://www.npmjs.com/package/repomap-mcp)；论文侧
  [RepoGraph (ICLR 2025)](https://arxiv.org/abs/2410.14684) 证明"仓库级图 + k-hop
  子图"平均相对提升 **32.8%**。
- **本仓现状**：没有"地图"工具；但积木齐全——`ide_outline`（符号）、
  `dep_graph`（跨文件引用）、`locate_edit`（引用计数）。缺的是**图上的排序**与
  **预算裁剪**——S102 已补。
- **落地（S102 实装）**：`repo_map`（search 域，rx-ide repomap 子命令 +
  `rust/src/repomap.rs`）——图：file --引用次数--> def、file --包含--> 自己的 def、
  def --1--> 所属 file；个人化 = focus 命中文件与其定义 ×50（aider 同款偏置），
  阻尼 0.85、30 轮幂迭代、悬挂节点按个人化向量重分配；定义复用
  `ide::symbol_spans`（四语言同口径），引用 = 全仓词法扫描命中已知定义名；
  输出 `map`（`相对路径:行 kind 名字`，rank 降序，预算 = 字符/4 近似）+
  `defs_total/defs_shown/tokens_est/truncated`。简化边界如实入文档：
  引用归属算给文件而非包含它的定义、同名定义共享权重、token 近似。
  实测（tools 域，focus=fs）：fs.py 的定义进入前 10（无 focus 时被跨文件
  高频引用者占据）；聚焦偏置与预算裁剪由 rust 5 测 + python 6 测锁定。

### 2. 混合检索 RRF：两条路各自会输的查询互补（✅ S101 已兑）
- **原理**：BM25（精确词元）与语义路（意图）失败模式不同；**Reciprocal Rank
  Fusion** `score = Σ 1/(k+rank)`，k=60 是业界默认，**不需要分数归一化**。
  代表：[QEX（Rust MCP 混合检索）](https://lib.rs/crates/qex-core)、BEIR 系列实验
  （[加权 RRF 复现](https://github.com/EgwDean/Query-Adaptive-Hybrid-Retrieval)）。
- **本仓现状**：`code_search`（BM25 行级）与 `code_semantic`（**rx-semantic.exe
  tf-idf 定义级**，非外部引擎）各自独立、没有融合——S101 已补。
- **落地（S101 实装）**：`code_search(hybrid=true)`：两路各取 `max(k*3,20)` 候选 →
  按 (file,line) 去重 → RRF k=60 融合 → 取 k 条；输出 `rrf`/`bm25_rank`/
  `semantic_rank`/`symbol`/`kind` + 顶层 `hybrid`/`rrf_k`/`paths`；语义路不可用时
  **显式降级**（`hybrid=false` + `degraded` 原因，BM25 结果完整），默认 false 时
  旧输出同形。实测：`sandbox resolve` 查询把语义路 #1 / BM25 #3 的
  `_resolve_in_sandbox` 顶到第一，压过只被 BM25 命中的注释行。
- **风险**：语义路不可用时的降级语义（已按"显式、不静默"落地）。

### 3. 查询侧根词约束：子词索引的假阳性防护（✅ S101 已兑）
- **原理**：索引期拆子词提召回（本仓已做），但**查询期也拆**会把 `HTTPSConnection`
  这类查询拆散，命中无关的 "handshake failed for Connection"——OpenObserve
  [PR #12324](https://github.com/openobserve/openobserve/pull/12324) 的实锤教训：
  **索引拆、查询不拆**。
- **本仓现状**：`rust/src/search.rs::tokenize` 索引与查询同用一套拆词——S101 已补。
- **落地（S101 实装）**：新增**资格门** `query_roots`——标识符类查询词取整词 +
  去分隔符连写变体（`parse_json` → `parse_json`/`parsejson`），文档**整词必须包含**
  某个根词才入选（子串判定保留前缀匹配与跨风格召回）；纯 CJK 查询无根词、门不生效。
  改前/改后对照实锤：`HTTPSConnection` 查询从 2 命中（含噪声文档）→ 1 命中（仅真定义）；
  `parse_json` 仍中 `parseJson`（跨风格保留）；`auth_gate_sweep` 仍中
  `AUTH_GATE_SWEEP_MARKER`（前缀保留）。rust 新增 4 测。

### 4. 内容寻址增量缓存：别每次重扫（✅ S103 已兑）
- **原理**：salsa/rust-analyzer 的增量计算（输入版本 → 记忆化查询 → 只重算受影响
  部分）；学术侧 [ECOOP 2025 增量静态分析](https://team.inria.fr/antique/reusing-caches-and-invariants-for-efficient-and-sound-incremental-static-analysis/)
  实测中位加速 **1.3–68×**；工业侧 Coverity+Bazel 把 12 小时降到 22 分钟。
- **本仓现状**：`ide_build` 有 8 条指纹缓存（先例）；scan/search 每次全量重扫
  （S83 因 exe 短命而退役 `_SCAN_CACHE`——**现在的问题不是 exe 短命，而是结果没落盘**）。
- **落地（S103 实装）**：`tools/cache.py` 进程内内容寻址缓存 + `registry.call`
  接线——键 = 工具+规范化参数+**cursor**+输入指纹；指纹 = 代码扩展名文件的
  (relpath, size, mtime_ns) + **≤256KB 文件内容哈希**（Windows 时钟粒度 ~15ms，
  同尺寸快速改写会假命中——测试实锤后加的内容哈希）；白名单只含 10 个纯读工具；
  `__no_cache`/环境变量旁路；越界调用不入缓存；LRU 128 条。
  实测：bug_scan 48→3.7ms、ast_scan 45→3.7ms（**约 12×**）；整仓 repo_map 命中
  收益较小（~1.8×，指纹要读全仓小文件）。边界如实入文档：大文件只按 size+mtime、
  >8MB 哈希预算或 >2 万文件不缓存、进程内不跨重启。
  全量测试实锤的坑：cursor 是传输层参数（算键前已被剥除）→ 第 2 页会命中第 1 页
  缓存，已把 cursor 纳入键并加回归测试。

### 5. 测试影响分析：只跑被改到的测试（✅ S104 已兑）
- **原理**：[Ekstazi（ISSTA 2015）](https://www.cs.umd.edu/~mwh/papers/ekstazi.pdf)
  用**文件指纹**记录每个测试的动态依赖，未变则跳过；实测测试时间 −32%
  （长测试 −54%）。粒度选择"文件级"是总耗时最优解。
- **本仓现状**：`ide_test` 每次跑全量——S104 已补。
- **落地（S104 实装）**：`ide_test(tia=true)`（仅 pytest）——pytest 插件
  `urx_tia_plugin` 用 stdlib audit hook 记录依赖（**收集期按文件 + 执行期按
  nodeid 两层**；`.pyc` 映射回源文件；路径统一 `/`），首次全量建图，之后
  `select()` 只跑依赖集与变更集相交的测试；**保守口径**：无依赖记录/新测试/
  收集失败 → 全量；无受影响测试 → 不执行并报 `skipped-no-impact`。
  依赖图进程内保存（与缓存同边界）。实测：改 `mod_a.py` → 只跑 `test_add`
  （selected 1/skipped 1）；无改动 → 跳过执行；`full=true` 强制全量。
- **开发中实锤的两个坑**：①只记执行期打开会得到**空依赖图**——测试模块在收集
  阶段就被 import，执行期不再 open；②二次运行打开的是 `__pycache__` 里的
  `.pyc` 而非 `.py`——必须映射回源文件，否则依赖图时有时无。

### 6. 切片式上下文包：给"依赖闭包"而不是"整窗"（P1）
- **原理**：程序切片（Weiser 经典）+ 现代变体——[ARISE](https://export.arxiv.org/pdf/2605.03117)
  用 def-use 边做前后向切片、按 token 预算组装证据包；SWE-Pruner 实测 token
  −23%~54% 而正确率不掉。
- **本仓现状**：`code_context` 是**行窗口**（radius），无依赖关系。
- **落地**：新参数/工具 `code_slice(symbol|line, direction, budget)`：基于 pyast
  （Python 真 AST）与自研 rust 信号做**近似 def-use**（变量/参数/字段名的读写集），
  输出"切片 + 省略说明"。验收：构造样例证明"切片含全部影响行"（与全窗对照）。
- **风险**：近似切片会漏（别名/动态属性）——必须如实标注"近似"并给全窗逃生口。

### 7. 栈图式名字解析：不依赖 LSP 的语义级引用（P2，最高杠杆）
- **原理**：[Stack Graphs（GitHub, EVCS 2023）](https://arxiv.org/abs/2211.01224)
  ——把名字绑定编码成图，解析引用 = 图上寻路；**纯语法**、无需构建、**文件级增量**，
  支撑 GitHub 全站精确导航。开源：[github/stack-graphs](https://github.com/github/stack-graphs)。
- **本仓现状**：`dep_graph`/`locate_edit` 是**文本引用**（含注释/字符串），
  `rust_reach` 是可达性分级，都不是绑定解析——这也是 VULN-HUNTING 附录 B 里
  "路径/数据流查不了"的根因之一。
- **落地**：先做**单语言简化栈图**（Python：模块/类/函数作用域 + import 绑定；
  Rust：mod/use/fn），只解"引用 → 定义"这一件事，喂 `ide_impact`/`ide_rename`/
  `dep_graph` 升级。分两轮：设计轮（作用域规则表 + oracle 场景）+ 实现轮。
- **风险**：这是"自研解析器"路线的延伸（本仓已有 pyast 先例），但语义覆盖面
  永远达不到编译器——定位是"比文本级准、比 LSP 轻"，文档必须写清边界。

### 8. ACI 输出纪律复核（P1）
- **原理**：[SWE-agent ACI 论文（NeurIPS 2024）](https://arxiv.org/abs/2405.15793)
  证明**只改接口设计**带来 +10.7pp；要点：动作简单、一步到位、反馈高信号、
  护栏防错。搜索输出"只列有命中的文件"、空输出显式消息、编辑后回显窗口。
- **本仓现状**：条数/字符串钳制、编辑语法门、窗口化读取**已有**。
- **待补**：①空结果显式说明（而非空数组）；②大结果**落盘 + 引用**
  （MCP 2025-06 的 `resource_link` 形态，或本地路径 + 10 行预览）；
  ③错误消息"可修复化"（指出下一步）。成本低，逐工具过一遍即可。

### 9. 漏洞知识库（Vul-RAG 式）（P2）
- **原理**：[Vul-RAG](https://arxiv.org/abs/2406.11147) 从 CVE 抽多维知识建库，
  检索后让模型"按成因与修复推理"：准确率 +12.96%，人工复核 60%→77%。
- **本仓现状**：`lesson` 是人工教训库（关键词检索），与规则解释面不连通。
- **落地**：先建**小规模种子库**（自仓历史 bug + 规则说明 + 修复样例，JSONL），
  给 `bug_scan`/`code_review` 的每条命中附"这类问题的成因/修法/先例"。
  数据获取是主要成本——建议只做自家历史 + 精选公开规则说明，**不追求全量 CVE**。

### 10. tree-sitter / ast-grep 作为**可选**结构引擎（P2）
- **原理**：[ast-grep](https://ast-grep.github.io/) 用代码模式（`$METAVAR`）做结构
  搜索/改写，20+ 语言；[Semgrep](https://semgrep.dev/) 偏安全数据流。
- **本仓现状**：自研 pyast/词法管线，语言覆盖 4+（ide 符号面）/ 17（lang_of 识别）。
- **落地**：按"单点接开源最强"的既有姿态——**探测到就用、否则回退自研**
  （与 LSP 同款降级纪律），不写入依赖红线。收益：语言覆盖与规则表达力。
- **风险**：外部工具行为差异会带来"同输入不同结果"——必须建对照 oracle。

### 11. SCIP 索引消费（P2）
- **原理**：[SCIP](https://github.com/sourcegraph/scip) 是 Sourcegraph 的紧凑索引
  格式（protobuf）；rust-analyzer 原生 `scip` 子命令可产出，Python 有 scip-python。
- **落地**：读 SCIP → 定义/引用/实现表，供 `ide_impact` 等消费；protobuf 解码可
  手写 varint（本仓已有手写 sha256/JSON 先例，符合零依赖红线）。
- **风险**：索引新鲜度（需用户侧生成）；先做"存在则优先、缺失则回退"。

## 三、建议路线（按杠杆/成本排序）

1. **S101**：查询侧根词约束 + RRF 混合检索（0.7 轮，立即见效，零依赖）。
2. **S102**：`repo_map` 工具（1 轮，agent 上下文选择的质变）。
3. **S103**：内容寻址增量缓存（1 轮，全工具共享，重复调用提速）。
4. **S104**：`ide_test` 测试影响分析（0.5–1 轮，日常开发直接受益）。
5. **S105**：ACI 输出纪律复核（0.5 轮，逐工具过）。
6. **S106+**：栈图式名字解析（设计轮 → 实现轮）——**最高杠杆但最重**，建议在前
   四项落地后启动；若只做一件"长期正确"的事，就是它。

## 四、明确不做 / 缓议（附理由）

- **稠密嵌入模型内置**（bge-m3 等）：违反零依赖红线；已有引擎桥接，保持"外部引擎
  可选"姿态即可。
- **微调本地模型**（LocAgent 路线）：用户明确不换模型（glm-5.3-flash 固定）。
- **全量引入 CodeQL/Semgrep**：重、与自研规则面重叠、且会带来"扫了=没有"的错觉
  ——与本仓 P1-b 矩阵的诚实口径冲突。
- **承诺"扫了=没有"**：红线（VULN-HUNTING 不做清单维持）。
- **宿主压缩策略改动**：用户定调不动 Yan Agent 本体。

## 五、来源

开源：[aider repomap](https://github.com/Aider-AI/aider/blob/main/aider/repomap.py) ·
[repo-mapper](https://github.com/mackenney/repo-mapper) ·
[repomap-mcp](https://www.npmjs.com/package/repomap-mcp) ·
[github/stack-graphs](https://github.com/github/stack-graphs) ·
[sourcegraph/scip](https://github.com/sourcegraph/scip) ·
[ast-grep](https://ast-grep.github.io/) ·
[QEX 混合检索](https://lib.rs/crates/qex-core) ·
[OpenObserve 子词教训](https://github.com/openobserve/openobserve/pull/12324) ·
[Spiral 标识符切分](https://github.com/casics/spiral)

论文：[Stack Graphs: Name Resolution at Scale](https://arxiv.org/abs/2211.01224) ·
[RepoGraph (ICLR 2025)](https://arxiv.org/abs/2410.14684) ·
[LocAgent (ACL 2025)](https://arxiv.org/abs/2503.09089) ·
[SWE-agent ACI (NeurIPS 2024)](https://arxiv.org/abs/2405.15793) ·
[Vul-RAG](https://arxiv.org/abs/2406.11147) ·
[Ekstazi (ISSTA 2015)](https://www.cs.umd.edu/~mwh/papers/ekstazi.pdf) ·
[增量静态分析 (ECOOP 2025)](https://team.inria.fr/antique/reusing-caches-and-invariants-for-efficient-and-sound-incremental-static-analysis/) ·
[SWE-Pruner](https://arxiv.org/abs/2601.16746) ·
[ARISE](https://export.arxiv.org/pdf/2605.03117) ·
[标识符切分 BiLSTM](https://arxiv.org/abs/1805.11651)
