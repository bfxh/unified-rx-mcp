# unified-rx-v2

**本地工具代替智能体体力活的平台** — 凡是 AI 要做的确定性体力活，全部下沉为本地工具；AI 只保留决策层。
> 定位：**工具箱，不是智能体，不是内核**。MCP 只是通道，价值在"工具 + 工作流"的完整链路。
> 七维"掌握"：**结构 / 语义 / 定位 / 探索 / 记忆 / 反馈 / 质量**
> 设计哲学：**少而准**（71 个工具，不用 183 个噪音）· **零依赖可跑**（纯 stdlib）·
> **写文件通道必须可靠**（fs_write 带授权直传）· **单点接开源最强**（语义引擎/LSP 不自研）·
> **库选型三问**（理念契合 > 版本前沿 > 省 token；本仓红线下的合法形态=探测薄壳，
> 协助开发其他项目同此纪律——[spec/LIBRARY-POLICY.md](spec/LIBRARY-POLICY.md)）

**当前 v2.65.0（S149）**：80 工具 / 14 域（**core 档 54 件**）；**渐进披露 + sys 通用化 + 提权路径**——
①**渐进披露（域 profile）**：`list_tools` 按启用域裁剪，越域调用给开启指引；
`profile_status`/`profile_enable` 恒在（后者**需授权**＝capability 变更批准，
对齐 OWASP MCP），开启后发 `notifications/tools/list_changed` 宿主重拉即见；
宿主侧 `UNIFIED_RX_PROFILE=core` 首屏 **54 件 ≈ 9.5K token**（全量 80 件 ≈ 13.6K），
core 档设 ≤30,000 字符**软帽**（实测 28,882，膨胀即红）。
②**sys 通用化（不只是游戏）**：`sys_steer` 目标支持 **pid 或可执行名子串**，档位＝
预设（render→P 核 / background→E 核）+ **显式组合** `class_=p|e|any` /
`priority=highest…idle` / `eco=on|off`——LLM 推理进程钉 E 核、浏览器主线程钉 P 核
都是一条命令；新增 `sys_procs`（按名找目标）与 `sys_privilege`（**需授权**）。
③**提权/访问权路径**：`OpenThread` 被拒（winerr=5）自动尝试 **SeDebugPrivilege**
并重试一次，仍失败则逐线程带错误码与人类可读原因（"需管理员运行 / 受保护进程
PPL"）；枚举不到线程**不再静默空转**而是明确报错（PID 4 实测）。
历史链：S147 审核三新门（历史明文/依赖红线/审计账本）+ 首个第三方仓审计（DeepSeek-Reasonix 报告 + 可移植套件）；历史链：S146 协议双支持（2025-06-18 +
顶层 title）与握手账本加固；（用户指令：不需要 GitHub/Linux，就地把审核搞强）：`scripts/local_gate.py`
一条命令跑完与 CI **同一套脚本**的全部门禁——快门 6 步（secrets / self-attack /
data-flow / toolface / tool-evals / selftest，**4 秒级**）与全门 9 步（+pytest 全量 +
cargo test + clippy）；`.githooks/` 版本化钩子（pre-commit 快门、pre-push 全门）经
`git config core.hooksPath .githooks` 一次安装——**审核在本机即可完整跑完，CI 降格为
镜像/备份**；本地门与 CI 不漂移（core.yml 出现的门脚本必须都在 local_gate 步骤里）
由测试锁死，`UNIFIED_RX_GATE_FORCE_FAIL` 注入必红做真门验证；②**协议协商 + 握手
留痕（B1 部分兑现）**：initialize 按规范协商版本（命中白名单回显、否则回我方最高
支持），并落 `~/.unified-rx/clients.jsonl`（客户端名/版本/请求版本/协商结果）——
**下个 ZCode 会话重启即取得宿主实际请求版本**（证据驱动，不猜）；③**审核流程两处
实锤修复**：`taint_gate.py --update-baseline` 曾把既有 why 全清成占位（改为按
(file,sink) 继承旧 why，只新条目落占位）、`audit_copy.py` 增加脏树护栏（副本必须
对应已提交状态，--allow-dirty 才放行）。历史链：S144 外部对标 B 档三件（B3 注入立场
14 件内容类工具 `[untrusted-content]` 前缀 + B2 任务级 evals 13 任务进 CI + B4 描述
−44%；**顺带实锤修复** `tools/list` 协议层不转发 annotations 的 S143 补遗）；
**S144 语种账结论：机器面向保持中文**。
对标全文见 [spec/EXTERNAL-ALIGNMENT.md](spec/EXTERNAL-ALIGNMENT.md)。历史链：S143
工具注解补齐 + 工具面体量仪表（`scripts/toolface_budget.py`，软帽 45,000）；
S141 烧量三件套：`burnwatch` 会话哨兵（model-io 体积越阈分级告警，破除"事后看
账单"）+ `session_burn` 工具（会话体积即查）+ 日计数跨重启持久化（`daily_state.jsonl`，
一日多启不再清零）；QPM 默认按实测标定 600→**3000**/60s（正常重度工作日峰值 ~1600
次/分钟不误伤，~10000 次/分钟的失控洪峰照拦）、日量告警 5 万→**10 万**；启动巡检可
用 `UNIFIED_RX_AUTOPILOT=0` 整体关闭。S140 全景：**数据流门进 CI + 首个外部选型体检**——
①**CI 第三道 dogfood 硬门**：`data-flow gate`（`scripts/taint_gate.py`）——产品面
definite 对照 `spec/taint-baseline.json` 基线，新增即红（入册须人工填 why，占位未填
被元锁拦截）；`scan.yml` 周扫扩成**审计三连**（secrets + attack + taint）；与
Secrets/Self-attack 并列为三道硬门。
②**LIBRARY-POLICY §六 首次跨项目落地**：对 `D:\开发\RUST WL`（vxl-phys 物理引擎）
出具只读选型体检（`docs/LIBRARY-AUDIT.md`）——三问逐件评估 + lock 全量归属反查 +
5 条收敛项（edition 2024 / MSRV / 依赖写法等），结论"极小面+纪律齐、无高危"；
格式即后续外部体检模板。历史链：S138 审计复审机制化（Self-attack gate + Mimosa
复审仪式脚本）；S137 按库分类清单 + copy-based 全量审计（实锤修复 local_run
shell=True→argv）；S125 `ide_callgraph` + CI 首绿（`SECRETS-GATE OK` / `CI-GATE OK` /
`EXE_TAG ok=9`）。本地 pytest 3.14 = 857 passed + 3 skipped；cargo **200 绿** + clippy 零告警；selftest 机器对账三行全绿
（VERSION_TAG / SKILLS_DOCS / EXE_TAG）。整合除重与升级路线见
[spec/CONSOLIDATION.md](spec/CONSOLIDATION.md)，现状坐标见
[spec/PANORAMA.md](spec/PANORAMA.md)，逐轮决策与证据见 [spec/ROUNDLOG.md](spec/ROUNDLOG.md)，
加固红线与 CI 门禁见 [spec/HARDENING.md](spec/HARDENING.md)。

## 与旧版 unified-rx-mcp 的关系

| | 旧 unified-rx-mcp | unified-rx-v2（本仓） |
|---|---|---|
| 工具面 | 183（注入面 200+） | **80 个组合工具 / 14 域**（core 档 54 件） |
| server | 7462 行上帝文件 | 协议薄层 + tools/ 按域 |
| 依赖 | mcp SDK + 多扩展 | **纯 stdlib 零依赖**（Rust 侧 `[dependencies]` 恒空） |
| 写文件 | 授权剥离（写不了） | `__authorized` 直传，可控 |
| 检索 | 5 套并行 | code_search 统一（可接 codegraph） |
| 代码智能 | 手写 AST 文本规则 | 结构化扫描层 + **真 LSP 客户端** + 23 件 ide 工具 |

## 工具面（14 域 · 80 工具）

| 域 | 工具 |
|---|---|
| 📁 fs (4) | `fs_read` `fs_write` `fs_stat` `fs_list` — 沙盒 fail-closed；读面纯 Python（S95 回迁，golden oracle 锁等价），写面 rx-fs.exe |
| 🐛 scan (13) | `bug_scan` `std_check` `ui_check` `bug_locate` `project_scan` `project_health`（S136 自 ops 归位：评分=三路扫描语义）`ast_scan` `code_review` `vuln_knowledge` `ast_grep` `file_scan` `near_dupes` `secrets_hunt`（S123：凭据泄漏扫描，掩码输出） — 正则 + AST-lite，非编译器语义；覆盖矩阵见 VULN-HUNTING 附录 B |
| 📐 metrics (3) | `code_coverage` `dep_graph` `module_stability` — 代码质量度量（S136 自 scan 域归位：模块 metrics.py 与组轴对齐；代码模式扫描仍属 scan） |
| 🖥️ sys (6) | `sys_topology`（P/E 核分级：EfficiencyClass 双 API 交叉，非混合平台如实报 uniform）`sys_threads`（线程优先级/理想核/CPU 集）`sys_steer`（**需授权**：render=关键线程→P 核 / background=后台→E 核，CPU Set 软定向 + EcoQoS，`hard` 走硬亲和并标代价）`sys_devices`（显示适配器，同类显示口去重）`sys_procs`（进程清单，按名找目标）`sys_privilege`（**需授权**：开 SeDebugPrivilege，跨进程改线程前置） |
| 🛠️ ide (23) | `ide_outline` `ide_read_symbol` `locate_edit` `code_context` `ide_edit_multi` `ide_batch_edit` `ide_rename` `ide_lsp` `ide_impact` `ide_diagnostics` `ide_build` `ide_test` `ide_debug` `ide_break` `ide_doctor` `ide_multi_check` `ide_vscode` `ide_auto_report` `ide_health_trend` `scip_refs` `ide_dead_code`（S123：死符号可达性）`ide_callgraph`（S125：真调用图）`ide_risk_rank`（S129：风险榜——高扇入×无测试排序） |
| 🔍 search (3) | `code_search`（BM25，`hybrid=true` 时与语义路 RRF 融合）`code_semantic`（tf-idf 定义级）`repo_map`（个人化 PageRank 符号地图） |
| 🛡️ guard (4) | `hallucination_guard` `capability_manifest` — 声明核查（读取过沙盒，S97）；`breaker_status` `breaker_reset` — **工具熔断**（同一工具+参数窗口内 >10 次即断，S122；S131 自 meta 域归位） |
| 🧠 learn (1) | `lesson` — 教训记忆（关键词检索，非向量） |
| ⚙️ ops (5) | `backup` `scan_log` `usage_stats` `session_burn`（S141：会话烧量监测）`lesson_stats` |
| 🎮 game (2) | `game_check` `blender_verify` |
| 🚀 engine (2) | `engine_status` `engine_query` |
| 🕵️ attack (6) | `input_fuzz` `path_probe` `big_input` `rust_taint_scan` `auth_gate_sweep` `attack_cruise`（S133：全攻击面一键巡航） — 自攻面常驻 |
| 🧬 appaudit (3) | `app_audit` `app_clone` `app_clean` |
| 🧰 meta (3) | `local_run` `process` `gpu_status` — 授权门控 / GPU 遥测 |

已于 S15 移除的废物面（证据驱动）：kb_query / chatlog_search / cmd_cheatsheet /
code_complete / ide_references / cost_report / trend_analysis / pipeline / parallel / pure_*。

## 词汇表（跨工具约定，S132/H2+M3）

| 约定 | 含义 | 例 |
|---|---|---|
| `root` | **项目根**（调用图/检索/风险榜等全仓语义工具的入口） | `ide_callgraph(root=…)` |
| `path` | **文件或目录**（扫描类工具入口，两种都收） | `bug_scan(path=…)` |
| `file` | **单文件**（定位/影响面类，必是文件） | `ide_impact(file=…)` |
| `kind` | **级别/类别**：bug_scan=实锤级别（definite/clue）；taint=可达性级别（+naive）；appaudit=类别 | `"kind": "definite"` |
| `engine` | **执行后端**：impact=结果来源档（lsp/resolved/text/callgraph）；filescan=计算引擎（rust/gpu/cpu） | `"engine": "resolved"` |
| `flow` | **污点链形态**：direct / interproc（文件内跨函数）/ cross（跨文件） | `"flow": "cross"` |
| `skipped` | **能力缺席如实上报**（不静默）：未装工具/超时/被拒 | 诊断面 linter 缺席 |
| `__authorized` | 执行·写类工具的**授权确认**（授权三档见 HARDENING §七） | `ide_build(__authorized=True)` |

约定：新工具按本表取词；存量两词汇（`root` 15 件 / `path` 28 件）不追溯改名——
破坏面大于收益（DESIGN-REVIEW H2 结论）。

## 运行

```bash
python server.py            # MCP stdio 模式
python server.py --selftest # 协议自检 + 机器对账（schema/版本/skills/exe）
python -m pytest tests/ -q  # 全量测试（当前 602 passed + 2 skipped 基线）
```

LSP 能力需要宿主装有对应语言服务器：

```bash
rustup component add rust-analyzer                            # Rust
python -m pip install python-lsp-server pycodestyle pyflakes   # Python
```

缺失时 `ide_lsp` 的 status 如实报 `detected=false` + reason（S99 修：`python -m`
形态必须验到模块层，不再因 exe=解释器而假阳性），`ide_impact` 自动降级为文本级
影响面（`engine="text"` + 精度标注）——绝不假装支持。

## Rust 原生化（S78 起）

16 件薄壳化：`fs_write`、search 双件、scan 五件、appaudit 三件、ide 五件；
其余结构性留 Python（判型在案，见 VULN-HUNTING）。fs 读面 S95 回迁纯 Python
（微秒级操作不付进程拉起溢价：fs_stat p50 7.7-9.7ms → 0.3ms）。
红线：`[dependencies]` 恒空；每轮 pytest + cargo test 双绿才准合入；
exe 缺失报清晰错误、不静默降级；沙盒语义两侧等价（WSL 面实测）。

## 评测体系（详见 spec/EVAL.md；H1-H4 台账在 §8）

| 假设 | 结论 | 关键数字 |
|---|---|---|
| H1 任务增益 | ✅ 双臂实测 | Δsolved **+6.7pp**（deepseek-chat n=90/90）、**+10pp**（glm-4.5-flash n=90/50）；**口径校正**：收益是"解决率 + 可核验性"（裸模型文件引用存在率 0% vs 工具组 63%/23%），不是"省 token"——轮次/成本反而升（§8） |
| H2 幻觉守卫 | ✅ 首测达标 | A 臂一致率 **1.0**（652 条）、B 臂 **0.9295**（766 条）；漏判（不存在却放行）**0**；分歧全为行级严判 |
| H3 扫描器查准 | ✅ 复测 PASS | precision≈1.0（api_key_sk 6/6；三条 WEAK(n=1) 黄灯如实亮着）；案底 FP 复检 0 命中 |
| H4 记忆复利 | ✅ 缩影 | 8 任务带教训复跑 solved 0/8→3/8、fail 点 -72%（复跑需 API 预算，挂账⑥） |
| H5 fail-closed | ✅ 固化 pytest | 安全模糊集 100% 拒绝 |

三阶段评测管线（bench/）：**L3** 双臂 A/B（ab_run.py，330 份答案入库）→
**P3** SWE-bench 外锚（swe_p3/swe_verify/swe_repair 三段）→ **P1** 标注 bug 库
（p1_build/p1_score）。延迟/内存基线见 §6（S94 立账）/ §7（S95 复测）。

## 施工史

S1-S26 全程对账见 [spec/UPGRADE.md](spec/UPGRADE.md)；S27 起逐轮见
[spec/ROUNDLOG.md](spec/ROUNDLOG.md)。近期弧线：Rust 原生化逐域推进（S78-S97）→
质量门禁机器化（S91-S94：selftest 三行对账 + 版本锁步）→ fs 读面回迁 + 高压电池
（S95）→ 副本深扫分诊（S96）→ 读面沙盒补漏（S97）→ 规则覆盖矩阵（S98）→
ide 检测/降级两修 + 文档刷新（S99）。
