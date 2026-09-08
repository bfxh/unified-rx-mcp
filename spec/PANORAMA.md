# PANORAMA —— 项目全景复盘与开发方向

> 用途：一份文档说清三件事——**推理主线**（为什么走到今天）、**现状坐标**（站在哪）、
> **开发方向**（往哪走）。写作时点：2026-09-08 @ v2.20.0（S94 质量体检轮）。
>
> 史料分工（读哪份文档管什么，见文末"七、文档地图"）：S38+ 逐轮对账见
> [ROUNDLOG.md](ROUNDLOG.md)；S10-S44 见 [UPGRADE.md](UPGRADE.md)；主题速览见
> [ACHIEVEMENTS.md](ACHIEVEMENTS.md)；评测口径见 [EVAL.md](EVAL.md)；扫描纪律见
> [SCAN-POLICY.md](SCAN-POLICY.md)；漏洞能力建设见 [VULN-HUNTING.md](VULN-HUNTING.md)。

## 一、项目是什么（一段话 + 红线）

**本地工具代替智能体体力活的平台**——凡是 AI 要做的确定性体力活（查/扫/定位/验证/
记忆），全部下沉为本地确定性工具；AI 只保留决策层。工具箱，不是智能体，不是内核；
MCP 只是通道。从旧版 183 工具/7462 行上帝文件/mcp SDK 多依赖，重写为 **57 工具/12 域/
纯 stdlib 零依赖 + Rust 原生加速面**。

**红线（不可协商）**：
1. Cargo `[dependencies]` 恒空——零第三方 crate，正则/JSON/AST/SHA256/FFI 全手写；
2. pytest + cargo test **双绿才准合入**；
3. exe 缺失清晰报错，**不静默降级**；
4. 沙盒纪律（fail-closed、`_fs_resolve` 语义）与授权门语义必须在 Rust 侧等价复刻，
   经 `auth_gate_sweep` 同款自审；
5. 语言版本政策（S79）：Rust 最新稳定工具链 + edition 2024；Python 3.14 主线
   （宿主实际解释器）+ 3.11 全绿回归网。

**协作纪律（宿主侧）**：Yan Agent 不改本体/压缩策略/模型（glm-5.3-flash）；
config.json 必须 Yan Agent 完全关闭后才动（改前备份+diff 校验）；禁自扫（Mimosa
深扫只跑副本）；不宣称项目安全；**智能体工具使用只走稳定版** `D:\rj\MCP\server.py`
——开发过程中任何智能体（含主控）调工具一律经稳定版入口，绝不 import 开发仓；
开发仓只用于开发与测试电池（S90 用户裁决，skills/workflow.md 原则 7）。

## 二、推理主线：六个时代（88 轮压成六篇）

### 时代一 · v2 重写与工具面定型（起点 ~ S18，2026-08 中旬）
**为什么砍**：183 工具是"注入面 200+"的噪音海，单文件 7462 行无法维护，mcp SDK
依赖链不可控。**决策**：12 域组合工具（36 @ S18）+ 纯 stdlib + 七维"掌握"设计
（结构/语义/定位/探索/记忆/反馈/质量）+ fs_write 授权直传（旧版授权剥离导致写不了
文件——写通道必须可靠）。**沉淀**：S9 结构化扫描层（py 真 AST）、S12/S13 缓存与
行重排、S15 证据驱动砍掉 10 个废物工具、S16 跨文件可达性归档、S17 真 LSP 客户端
（不自研语义，单点接开源最强）。

### 时代二 · 评测驱动与外锚（S19 ~ S37，08-27~28）
**推理**：没有度量就没有升级——先建 EVAL 体系，再接 **SWE-bench Verified 外锚**
（S21-S25）。**最大教训（S22）**：B 臂 34% "solved" 是散文 patch 通胀——16/16
补丁 0 个可 apply；执行口径（真 fail-to-pass 实跑，S24）落地后 H1 fix-equivalent
增益归零，真实增益收缩到定位面（same_root 72.3% vs 63.8%）。**这条教训定了全项目
的记账基调：一切增益必须过执行口径，散文自评不算数。** 同期：bug_scan P/R 人工
标注双轮（S26-S27）、WSL 执行环境 C 扩展仓入局（S28-S30，feasible 44/47）、
IDE 域建成（S32-S37 编译/调试/断点/clippy/诊断回喂修复轮）。

### 时代三 · 诚实收口与守卫硬化（S38 ~ S53，08-29~30）
**推理**：信号研究该停就停——S38 三路信号 A/B 实测 net lift 0，**负结果如实入账
不硬凑**，S47 复测净 −2 后收口"不再 revisit"。**用户逼出来的大鱼（S42）**：WSL
测试假跑（tail 尸检发现未定义变量）→ 45 个 FTB 全是假的 → 修复 + S43 守卫全面
硬化（能力探针替代存在性检查）+ S43b 缺省沙盒全开修复（fail-closed 恢复 S0 语义）。
**自食闭环（S44-S45）**：code_review 多透镜评审 + 12 复杂度热点全清零——工具链
先吃自己的狗粮。**S53 维稳版部署**：clone 到 D:\rj\MCP + tag v2.3.0 + workflow
流程固化（feat 分支→双绿→PR→tag→同步稳定版）。

### 时代四 · IDE 扩容与默认严苛（S54 ~ S71，08-30~31；spec 逐轮记录缺口，git 补账见"四"）
**推理**：工具面从"查"扩到"改"必须有防护网——S56 写前防护 + 三次同源静默死亡的
全局守卫、S57 ide_test 统一入口、S58 rename_apply 落盘+影响面、S59 ide_doctor 一键
体检、S65-S66 代码评审透镜过滤/ide_batch_edit/ide_outline。**同期两轮自查**：
S60 挖洞轮五个真 bug、S61 硬化轮六项、S62 默认严苛轮（入站上限、**fs_write 原子写
tmp+replace**、原则制度化）。**S69-S71 autopilot**：server 启动即自动体检+调试、
快照持久化+健康趋势。工具数 45→54。

### 时代五 · 权力面盘点与 Rust 迁移决策（S72 ~ S78，08-31~09-05）
**推理**：宿主侧诊断实锤"token 被吞 + 多轮修不到根因"→ S72 错误可修性三连修
（error_detail 堆栈尾部/钳制嵌套递归/local_run UTF-8 解码）。S73 深扫三真修 +
**读路径同样过沙盒**纪律成文（SCAN-POLICY）。S75 权力面全面盘点（55 工具 ×
授权门/沙盒/执行点三列交叉）：门控看"能力"——写/执行/提权才设门，纯读=本职；
manifest 高权限段动态生成。S77 auth_gate_sweep 自审工具**首跑即抓到 ide_lsp 假门**
（manual_gate 机制由此诞生）。**S78 用户决策"PY换Rust"**：rust workspace 零 crate
起盘——手写 JSON（i128 保真）、污点引擎、MCP 协议层；S73 人工 triage 从此机器化
（入口污点模型：@tool 装饰=宿主可达边界）。

### 时代六 · 原生化冲刺与宿主接入（S79 ~ S90，09-05~07）
**迁移方法论在本篇定型**（每域同款）：
**薄壳转调 + oracle 对照实验（删码前逐字节 PARITY）+ 双解释器 pytest/cargo 双绿 +
退役断言（内部名不得复活）**。
- S79 fs 读三件（发现 walk 深度语义契约 + 政策入 workflow）；
- S80-S81 search 域全薄壳（**最大发现：200 文件截断顺序有契约**——遍历序错则
  N/df 全变分数系统性漂移；**缓存负资产论**：短命 exe 冷调反而快一倍）；
- S82-S84 scan 域全薄壳（godot/unity 正则跨行语义、f-string PEP 701、**CRLF
  通用换行坑**——探针必须带真实行尾；oracle 漂移纪律：被扫仓库内文件改完必须
  重生成 oracle）；手写 pyast.rs 迷你解析器（~3000 行，3.14 语义）；
- S85-S86 appaudit 域收官（**Python 3.14 walk 真值**：junction 不再是 symlink；
  reparse tag FFI 实锤；py_int 逐字复刻防位截断变号）；**判型决策：attack 活体
  自审/外部进程编排/宿主内省结构性不迁**；
- S87 终点改写：**宿主入口 = python server.py 编排器**（薄壳转调使转发代理非必需），
  GUI 实测"连接成功，57 个工具"；
- S88 三路排查（Mimosa 副本深扫 + attack 五件套 + 人工精读，交叉收敛才动手）：
  实锤 8 个读取工具漏钳制 + project_health 假满分，S73 纪律从纪律变代码；
  **junction 逃逸实测已闭**并固化为回归；
- S90 fs_write 收官（**stdin 二进制字节通道**：subprocess text 模式 stdin 会做
  \n→os.linesep 换行翻译，探针实锤；scan/search 同源通道顺带修；fs 域 4/4 全薄壳，
  授权门仍留 registry）。同轮用户裁决入 workflow 原则 7：**智能体工具使用只走
  稳定版入口**。

## 三、关键决策账（决策 → 理由 → 今日状态）

| # | 决策 | 理由（推理） | 状态 |
|---|------|--------------|------|
| 1 | 工具面收缩 183→36→57 组合工具 | 噪音海不可维护；少而准 | 稳定，57/12 域 |
| 2 | 纯 stdlib / Cargo 恒空 | 依赖链可控、单文件可部署 | 红线执行中 |
| 3 | 沙盒 fail-closed（缺省=拒） | S43b 端到端验收抓出缺省全开 | 稳定，双语言等价 |
| 4 | fs_write 授权直传 | 写通道必须可靠（旧版写不了） | S62 起原子写 |
| 5 | 门控看能力：写/执行/提权才设门，读路径沙盒钳制兜底 | 纯读=本职，设门过度则误伤 | S73/S75/S88 三轮校准 |
| 6 | 授权门结构性留 Python registry | exe 永不自行放权（单一裁决点） | S86 定型 |
| 7 | 薄壳转调（python 编排器入口） | 转发代理非必需：入口自动获得 Rust 实现 | S87 落地，转发代理缓议 |
| 8 | oracle 对照实验后才删码 | 行为等价必须机器证明 | S80-S86 每域执行 |
| 9 | 退役断言（内部名不得复活） | 防死代码回潮 | S80-S86 每域执行 |
| 10 | 缓存负资产论 | 短命 exe 无跨调缓存面，冷调实测更快 | S80-S82 实测定案 |
| 11 | 诚实记账（负结果/止损/进程开销照写） | 假增益比没增益更贵 | S38/S41/S82 范例在案 |
| 12 | 禁自扫 + 三路交叉收敛 | 静态只是初筛；结论必须独立线互证 | SCAN-POLICY/S88 |
| 13 | 最新语言版本政策 | 用户指令 + 宿主实际解释器 3.14 | S79 成文 |
| 14 | 真 LSP/语义引擎单点接开源 | 不自研语义（codegraph/ra/pylsp） | S17 起稳定 |
| 15 | 智能体工具使用只走稳定版入口 | 开发中代码未经双绿+合入不得进智能体工具链 | S90 成文 |

## 四、记录缺口补账（S54-S71，git log 补）

ROUNDLOG 由 bench/log_round.py 自 S38 起追加，但 **S54-S71 十八轮未入 spec/**
（仅 git commit 单行）——逐轮决策细节已不可考，主题如下（来自 git log，
工具数为 commit 口径）：

| 轮 | 主题（git 口径） | 工具数 |
|----|------------------|--------|
| S54 | metrics 域：code_coverage（stdlib trace）/dep_graph（import 图+环检测）/module_stability | 45 |
| S55 | risky 模块测试补全 29→0 | 45 |
| S56 | R1 IDE 写前防护 + 全局守卫 | 45 |
| S57 | R2 ide_test 统一测试入口 | 46 |
| S58 | R3 rename_apply 落盘 + ide_impact 影响面 | 47 |
| S59 | R4 ide_doctor 一键体检 | 48 |
| S60 | 挖洞轮：五个真 bug（探针实锤） | 48 |
| S61 | 硬化轮六项 + 两环境/契约 bug | 48 |
| S62 | 默认严苛轮：入站上限/原子写/原则制度化 | 48 |
| S63/S64 | cargo workspace 计数修复 / _func_spans 两测量缺陷 | — |
| S65 | code_review lens 过滤/测试区跳过 + ide_batch_edit | 49 |
| S66 | ide_outline/ide_read_symbol + ide_doctor diff 模式 | 51 |
| S67 | 无单独 commit 笔录（如实记缺） | — |
| S68 | VS Code 后手入口 + 多项目联动体检 | 53 |
| S69 | autopilot：启动即自动体检+调试 | 54 |
| S70 | 输出钳制保头保尾 + 类型符号导航 | 54 |
| S71 | autopilot 快照持久化 + 健康趋势 | 54 |

**教训两条**：①log_round.py 断档一轮就断档一段——ROUNDLOG 应随 PR 流程强制补记
（S89 起恢复"提交前必有本轮条目"）；②S53-S71 期间 serverInfo 版本停更，靠
84034eb 事后对齐 2.5.6——版本账本需要机器对账（见"六、开发方向"#2）。

## 五、现状坐标（2026-09-09 @ v2.27.0）

**工具面 58/12 组**（selftest 口径）：appaudit(3) attack(5) engine(2) fs(4) game(2)
guard(2) ide(19) learn(1) meta(2) ops(5) scan(10) search(3)。

**Rust 原生化进度**：16 个工具已薄壳化（fs_write、
search 双件 code_search/code_semantic、scan 五件 bug_scan/std_check/ui_check/
bug_locate/ast_scan、appaudit 三件 app_audit/app_clone/app_clean、ide 五件
ide_outline/ide_read_symbol/locate_edit/ide_rename/code_context）；
fs_read/fs_stat/fs_list 曾于 S79 薄壳化、**S95 回迁纯 Python**（读面微秒级操作
不付进程拉起溢价：fs_stat p50 7.7-9.7ms→0.3ms，等价性由 golden master oracle
40 场景锁定，EVAL §7）；另有
rust_taint_scan 与 code_review 的 bug_scan 透镜经 exe 路径。**结构性留 Python**
（判型在案，S85/S87/S91）：attack 五件（活体自审——攻击对象就是运行中的 registry,
exe 化测错对象）、ide 余 14 件（LSP/编译/调试=外部进程编排 + registry/文本计算混合，判型表 S91）、
ops 副作用面、meta 宿主内省、game 外部编排、learn 小+写、guard、engine 探测、
授权门本体（registry.call 单一裁决点）。

**测试资产**：pytest 3.14 = 634 passed + 2 skipped ／ 3.11 = 636 passed；cargo
131 绿零告警（lib 21 + 各 exe 集成测，ide_test 19 = S92 12 + S93 7，bin_version_test
= S94，fs_test 并发回归 = S95，search_test 资格门 4 = S101，repomap_test 6 = S102）；selftest 58/12/SCHEMA_BAD 0 + 机器对账三行
（VERSION_TAG / SKILLS_DOCS S91、EXE_TAG S94）；junction 逃逸回归（S88）；stdin 通道
parity 三测（S90，argv vs stdin 强制等价）；S73 重放验收常驻（S78）；协议 fuzz
双靶 32 测（S78）；S92 对照实验 43 场景 + S93 对照实验 51 场景 masked 全等
（temp\s92、temp\s93 oracle 三件套）；S95 golden master oracle 40 场景 +
高压电池 8 测（tests/test_s95_stress.py）+ Linux 面 smoke 3 测
（tests/test_s95_linux_smoke.py，WSL 实测 fail-closed/沙盒内双态）；
S97 沙盒钳制补漏 4 测（ast_scan / hallucination_guard，test_s88_sandbox_clamp 16/16）；
S99 ide 两修 5 测（LSP 检测诚实化 + ide_impact 文本降级，test_s99_ide_fallback）；
S101 检索 5 测（查询资格门 rust 4 + RRF 融合 python 4，test_s101_search_hybrid）；
S102 repo_map 11 测（rust 图/聚焦/预算/四语言 5 + python 注册/形状/聚焦/预算/沙盒/exe 6）；
S103 增量缓存 11 测（命中逐字节一致/失效/参数分区/旁路/越界不入/写工具不入/
cursor 分页不串页/指纹口径，test_s103_cache）；
S104 TIA 6 测（首次全量→增量→无变更跳过/对称选择/新测试必跑/强制全量/
非 pytest 如实报/选择保守性，test_s104_tia）。

**质量体检基线（S94 立账，S95 复测，详见 EVAL §6/§7）**：延迟热态 fs_stat
p50 **0.3ms**（S95 回迁后，<10ms 预算余量 ~30 倍；v2.20.0 exe 路由期 7.7-9.7ms
贴地板为历史基线）、ast_scan 100 文件 50→34ms（预算 2s，余量 30 倍）、
engine_query 34-52→25ms（预算 15s，余量 300 倍）；内存基线 27.8MB、15 轮 soak
Δ+0.1MB 无泄漏信号（bench/s94_perf.py 留档滚动历史）；架构健康复核无上帝对象
（tools 最大 lsp.py 705 行，rust 最大 pyast.rs 2989=解析器合理单体）。

**部署拓扑**：开发仓 `D:\开发\unified-rx-mcp`（GitHub bfxh/unified-rx-mcp）→
稳定克隆 `D:\rj\MCP`（origin 指开发仓，git ff 同步）→ 宿主 config.json mcpServers
unified_rx 条目（`python -X utf8 D:\rj\MCP\server.py`，沙盒 `D:\开发;D:\rj\MCP`，
PYTHONUTF8=1）→ Yan Agent GUI 实测 57 工具连接成功。

**已知滞后（挂账）**：①宿主 config.json 描述串写 v2.14.0（Yan Agent 运行中不动
config，下个关闭窗口顺带更正）；②S54-S71 逐轮记录缺口（本文"四"已补主题账）；
③~~S93 的 GitHub 侧未推~~（**S94 轮内已清**：GitHub 直连恢复，feat/s93+feat/s94
两分支与 v2.19.0/v2.20.0 两 tag 已推、PR #57/#58 已合、main=e55bf28，本地与稳定
版均已 ff 对齐；tag 落分支尖与 merge 惯例的偏离随 PR 合并自然消解）；
④~~fs_stat 预算决策~~（**S95 已拍板并落地**：选方案①回迁纯 Python，
fs_stat p50 0.3ms 余量 ~30 倍，golden oracle 锁等价，数字在 EVAL §7）；
⑤Mimosa 语义层覆盖缺口（S96 副本深扫：静态层 193/193 代码文件完整、58 条分诊
完毕且 S95 候选面零命中，但 threatModel/findingDiscovery 阶段 partial——
`runStatus=inconclusive`，见 VULN-HUNTING S96 注记；待插件侧可完整跑通后对副本
复扫清账，期间不作安全宣称）；⑥H1/H4 的 A/B 复跑需 API 预算（H1-H4 台账已归档
EVAL §8：H2/H3 零成本复算在账，H1 数字出自 S14 已花账、H4 出自 S20 缩影；
H1 口径已校正为"解决率增益+可核验性"，非"省 token"）。

## 六、开发方向（建议排序）

### 近期 · 小而快（各一轮可完成）
1. **config.json 描述串更正 + 纪律补丁**：v2.14.0→2.15.0；workflow 补一条"改
   config 必带版本对账"（防再现 serverInfo 停更式漂移）。等 Yan Agent 关闭窗口执行。
2. **selftest 扩两项机器对账（✅ S91 已兑）**：①SERVER_VERSION ↔ 最新 git tag
   （OK/NEXT/DRIFT/SKIP 四态——落后=真实漂移信号，84034eb 教训工具化）；
   ②skills/*.md ↔ registry 工具名对账（陈旧名 stale + 零命中文件 dead，S88 手工
   补契约声明的教训工具化）。selftest 打印两行对账，不改退出码。
3. **bench 扫面噪音治理**：Mimosa 每轮 44 条 bench/ 命中是恒定分诊噪音（S50 已
   定性不砍代码）——SCAN-POLICY 补"bench 命中一律先按脚手架定性"或给扫面豁免标记，
   把深扫分诊成本降下来。

### 主线 · 既定路线图（Rust 迁移继续）
4. **fs_write 原生化 → fs 域 4/4 收官（✅ S90 已兑）**：授权门留 registry（决策 #6
   不动）、Rust 侧复刻 tmp+replace 原子写、oracle 12 场景 12/12 PASS。探针副产物
   成纪律：**跨进程传内容走二进制 stdin 字节通道**（text 模式 stdin 有换行翻译），
   scan/search 同源通道同轮归一。
5. **ide 域判型普查（✅ S91 已兑）→ ide_read 双件原生化（✅ S92 已兑）→ 定位三件（✅ S93 已兑）**：19 件逐件立表（表在 VULN-HUNTING 五）——
   可迁 5+1，结构性留 13（编排面是职责不是债务）。S92：ide_outline/
   ide_read_symbol 走 rx-ide.exe（_symbol_spans 四语言手写复刻，oracle 43/43）；
   S93：locate_edit/ide_rename/code_context 并入 rx-ide（遍历+搜索与 rx-scan
   基建同构，oracle 51/51）。ide 域可迁面收官（余 ide_health_trend 低优缓，
   低频聚合不值得单开一轮）。
6. **engine 域双实现归一**：BM25/语义引擎已住 rx-search/rx-semantic，engine_query
   的 Python 降级路径与 exe 直连归一（去第二实现面），engine_status 保持探测壳。

### 中期 · 量化收益（吃 ROI）
7. **H1-H4 全指标复测一轮（S94 性能/内存/架构 + S95 H2 首测 + S97 H1-H4 台账
   归档已兑；A/B 复跑挂账⑥）**：Rust 化延迟收益目前零散在账（code_search 930→140ms、
   semantic 930→330ms、std/ui 20-30%），S94 实测 code_search 33-87ms 保持、
   语义路径冷热敏感（591-1767ms），S95 复测全部预算 PASS 且 fs_stat 回迁后
   0.3ms（EVAL §7）；H2 幻觉守卫一致率首测 A 1.0 / B 0.9295、漏判 0
   （bench/h2_guard_eval.py，L3 答案复用零 API 成本）；H1-H4 台账（定义/最新
   实测/数据入口/缺口）已归档 EVAL §8；剩余=完整 A/B 复跑（需 API 预算，S38
   基线保留可复测）——回答"原生化到底买到了什么"。
8. **VULN-HUNTING P1-b/P2 兑现**：~~规则覆盖矩阵~~（**S98 已兑**：附录 B，
   5 语言+其他识别语言 × 7 类目逐格标 有规则/查不了/空白，"查不了"写明原因）；
   ~~tag 前深扫常态化~~（**S98 起执行**：附录 B 尾注流程 + S96 首样）；
   调用图定位（P2-a）仍挂——需设计轮。
9. **SWE-bench 外锚复跑一轮**：工具面自 S25 后大改（57 工具 + Rust 面），外锚
   回归参考价值高；bench 电池保留可用。
10. **外部技术雷达兑现（S100 立账，见 [ADVANCES.md](ADVANCES.md)）**：11 项
   候选按杠杆/成本排序——~~P0 查询侧根词约束~~ + ~~RRF 混合检索~~ + ~~repo_map~~
   （**S101/S102 已兑**）；~~P1 内容寻址增量缓存~~（**S103 已兑**，实测 12×）+
   ~~P1 测试影响分析~~（**S104 已兑**）；余 P1=ACI 输出纪律复核；P2=栈图式名字
   解析（最高杠杆最重，建议前四项后启动）、漏洞知识库、tree-sitter/ast-grep
   可选引擎、SCIP 消费。每项落地仍走 oracle → 双绿 → 文档。

### 缓议维持（有意识不做，防范围蠕变）
- **转发代理/单 exe 入口**：python 编排器入口零损失，双进程桥接复杂度不值
  （S87 决策原文维持）；
- **重型 SAST 进仓 / 宿主自扫 / 低质规则凑数 / "扫了=没有"承诺**（VULN-HUNTING
  明确不做清单）。

## 七、文档地图（哪份文档管什么）

| 文档 | 管什么 | 什么时候读 |
|------|--------|-----------|
| 本文 PANORAMA.md | 推理主线 + 决策账 + 现状 + 方向 | 新会话开局 / 迷路时 |
| ROUNDLOG.md | S38+ 逐轮决策/证据对账 | 查某轮为什么那么做 |
| UPGRADE.md | v2→v2.5 施工方案 + S10-S44 逐轮 | 查早期轮与升级方案 |
| ACHIEVEMENTS.md | S22-S31 成果主题速览 | 快速了解"前面的作为" |
| EVAL.md | 评测口径（H1-H4/L3/外锚） | 讨论增益/复测前必读 |
| SCAN-POLICY.md | 扫描纪律（禁自扫/副本/复合验证） | 任何扫描动作前必读 |
| VULN-HUNTING.md | 漏洞能力建设路线 + 终点形态 + S88 注记 | 安全相关施工前必读 |
| README.md / spec/README.md | 项目定位 / 工具契约（早期口径，工具数以 selftest 为准） | 对外介绍 / 契约速查 |
| skills/*.md | 12 域工具使用契约（含 S80+ 契约变化注记） | 写工具调用前 |
| workflow.md | 每轮流程（圈定→对照→实现→双绿→文档→PR→同步） | 每轮开工前 |
